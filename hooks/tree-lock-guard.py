#!/usr/bin/env python3
"""Deny a dispatch that would grant a second agent the same working tree.

Runs on PreToolUse for `Agent`/`Task` (a fresh dispatch) and `SendMessage`
(resuming an already-spawned teammate — the shape that actually collided:
"the lead dispatched a whole-tree claim-verifier and, in a message sent in
the same breath, told the dev teammate to start implementing" was two
grants, but only one of them was necessarily a fresh `Agent` call). Both
paths are covered by the same rule: is the target of *this* call a role
that mutates the tree, and does the lock already say someone else holds it.

Deliberately does not try to classify intent from prompt text. The two
signals it uses — `subagent_type`/`name` on an Agent call, `to` on a
SendMessage call — are structured fields, not prose, because this project
has already paid for the alternative twice tonight: a fuzzy match on free
text is a false-positive trap (same reasoning the skill's own `answered`
rule gives for why a `queued`-row text-match was rejected), and a false
"no conflict" here is worse than no check at all, since it would clear the
exact collision this exists to catch.

`isolation: "worktree"` on an Agent call is exempted outright — a worktree
dispatch never touches the shared checkout, so it was never a candidate
collision regardless of role.

Decides "deny", not "ask": this needs to hold even in an unattended run,
and a denial is a crisp, checkable signal (`permissionDecision: "deny"`)
in a way a human confirmation prompt is not. The reason string names who
holds the tree, since when, and how to proceed — release the lock, or
dispatch with `isolation: "worktree"` — so a denial is never a dead end.

On the non-conflict path this auto-acquires (or refreshes) the lock for
the new holder — the lead never has to remember a separate "claim the
tree" step, because a rule that depends on the lead remembering an extra
action is the exact shape of instrument this file exists to replace.

Never errors the calling tool call on an internal failure of this script
itself (a broken guard must not be able to block the session it guards) —
but that is different from denying a real conflict, which is the one
decision this file exists to make and must not swallow.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(
    os.environ.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board"
).expanduser()

# Same "half a working day" reasoning as tree_lock.py's own constant —
# duplicated, not imported, so this hook has no import-path dependency on
# where status-board/ sits relative to hooks/. Both must be changed
# together; a mismatch here means the hook and the board disagree about
# what "still held" means, which is a defect worth being visible about,
# not one worth hiding behind a shared import.
STALE_AFTER_MIN = 240

# team-roles.md's own categories, not invented here: dev writes src/**;
# claim-verifier's brief can include a full build+test+parity run, which
# is the exact "whole tree" shape that collided (over-inclusive on
# purpose — a claim-verifier task that turns out to be read-only costs one
# unnecessary check, a mutating one this list missed costs a repeat of
# tonight); git-op mutates .git and the index. architect and qa are
# read-only by norm and deliberately excluded; repo-inventory is
# explicitly read-only. A custom-named teammate is not in this set and
# will not be caught — stated here rather than left implicit.
TREE_MUTATING_ROLES = {"dev", "claim-verifier", "git-op"}


def slug_for(repo: Path) -> str:
    return repo.name.lower().replace(" ", "-")


def lock_path(repo: Path) -> Path:
    return STATE / "boards" / slug_for(repo) / "tree-lock.json"


def age_minutes(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 60)


def read_status(repo: Path) -> dict:
    path = lock_path(repo)
    if not path.exists():
        return {"held": False}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"held": False}
    if not isinstance(data, dict) or not data.get("holder"):
        return {"held": False}
    mins = age_minutes(data.get("since"))
    stale = mins is not None and mins > STALE_AFTER_MIN
    return {**data, "held": not stale, "stale": stale, "ageMin": mins}


def write_lock(repo: Path, holder: str, role: str, reason: str) -> None:
    path = lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "holder": holder,
        "role": role,
        "reason": reason,
        "grantedBy": "tree-lock-guard (auto)",
        "since": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def classify(tool_name: str, tool_input: dict) -> tuple[str, str, str] | None:
    """(holder, role, reason) for a dispatch that claims tree ownership, or
    None if this call is not a tree-ownership candidate at all."""
    if tool_name in ("Agent", "Task"):
        if tool_input.get("isolation") == "worktree":
            return None  # never touches the shared checkout
        role = tool_input.get("subagent_type") or ""
        if role not in TREE_MUTATING_ROLES:
            return None
        holder = tool_input.get("name") or role
        reason = str(tool_input.get("description") or "")[:200]
        return holder, role, reason
    if tool_name == "SendMessage":
        to = tool_input.get("to") or ""
        if to not in TREE_MUTATING_ROLES:
            # Only literal role-named recipients are recognised — a
            # custom-named dev teammate is a stated gap, not a silent one.
            return None
        return to, to, "resumed via SendMessage"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # no readable input — nothing this hook can decide safely

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or Path.cwd())

    claim = classify(tool_name, tool_input)
    if claim is None:
        return 0  # not a tree-ownership candidate; no opinion
    holder, role, reason = claim

    current = read_status(cwd)
    conflict = (
        current.get("held")
        and current.get("holder")
        and current.get("holder") != holder
    )

    if conflict:
        since = current.get("since", "unknown")
        age = current.get("ageMin")
        age_txt = f"{age:.0f} min" if isinstance(age, (int, float)) else "an unknown time"
        msg = (
            f"Working tree is already held by '{current.get('holder')}' "
            f"({current.get('role', 'unknown role')}), granted by "
            f"{current.get('grantedBy', 'unknown')} {age_txt} ago (since {since}), "
            f"reason: {current.get('reason') or 'none recorded'}. "
            f"Dispatching '{holder}' ({role}) now would grant the tree to a "
            f"second agent while the first still holds it — the exact collision "
            f"already logged twice in this project. Release the lock first "
            f"(`tree_lock.py --repo <repo> release`) if the first agent is "
            f"actually done, or dispatch with isolation: \"worktree\" if this "
            f"one does not need the shared checkout."
        )
        print(
            json.dumps(
                {
                    "systemMessage": msg,
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": msg,
                    },
                }
            )
        )
        return 0

    # No conflict — this dispatch becomes (or remains) the holder.
    try:
        write_lock(cwd, holder, role, reason)
    except Exception:
        pass  # failing to record the grant must not block a clean dispatch
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
