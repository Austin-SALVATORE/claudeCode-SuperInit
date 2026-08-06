#!/usr/bin/env python3
"""Working-tree ownership lock — the mechanism, not the rule that kept
getting re-broken.

`.claude/rules/team-roles.md` says "one agent owns the working tree at a
time" and `verification.md` repeats it — both were true, known, and were
being actively quoted by the lead while granting the tree to two agents
at once. Twice: a board finding titled "I told two agents at once that
they had the checkout to themselves", and — the occurrence that produced
this file — a whole-tree claim-verifier (full build + suite + served
parity run) colliding with a dev mid-implementation, the correcting
message arriving after files already existed. A rule that has been read
and re-broken is proof the rule is the wrong instrument (owner's ruling,
7 Aug, memory `trace-defects-back-to-the-plugin`): tree ownership lived
only in the lead's head and in prose, so nothing could catch the lead
granting it twice.

This file is the state, not the enforcement. `hooks/tree-lock-guard.py`
is what actually stops or surfaces a collision at dispatch time; this is
the one file both it and the status board agree on, so neither can drift
from the other's idea of who holds the tree.

    python3 tree_lock.py --repo <path> status
    python3 tree_lock.py --repo <path> acquire --holder dev --role dev \\
        --reason "implementing the write path" [--granted-by "team-lead"]
    python3 tree_lock.py --repo <path> release

Lock lives at boards/<slug>/tree-lock.json, alongside narrative.json,
backlog.json and answers.json — same per-project directory, one more
small file with one exclusive owner: this script and the hook, never a
human editing it by hand and never narrative.json's author. Same
two-files-two-authors posture the rest of this plugin already uses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(
    os.environ.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board"
).expanduser()

# A lock older than this is treated as abandoned, not held — same "half a
# working day" reasoning as HANDOFF_STALE_AFTER_MIN and
# SECTION_STALE_AFTER_MIN in status-board/index.html: long enough that a
# real build+test+parity run (the exact case that collided) has clearly
# had time to finish, short enough to still protect the next dispatch if
# the holder never explicitly released. A lock that can never expire turns
# one missed release into a permanent false alarm — the guard-nobody-
# trusts failure, from the opposite direction.
STALE_AFTER_MIN = 240


def slug_for(repo: Path) -> str:
    # Must match serve.py, board-answers.py, arm-watcher.py exactly, or
    # this reads/writes a board that does not exist and silently does
    # nothing forever.
    return repo.name.lower().replace(" ", "-")


def lock_path(repo: Path) -> Path:
    d = STATE / "boards" / slug_for(repo)
    d.mkdir(parents=True, exist_ok=True)
    return d / "tree-lock.json"


def read_lock(repo: Path) -> dict | None:
    path = lock_path(repo)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("holder"):
        return None
    return data


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


def status(repo: Path) -> dict:
    """The current lock, with staleness already resolved — callers never
    re-derive `is_stale` themselves, so the hook and the board cannot
    disagree about what counts as still held."""
    lock = read_lock(repo)
    if lock is None:
        return {"held": False, "stale": False}
    mins = age_minutes(lock.get("since"))
    stale = mins is not None and mins > STALE_AFTER_MIN
    return {
        "held": not stale,
        "stale": stale,
        "holder": lock.get("holder"),
        "role": lock.get("role"),
        "grantedBy": lock.get("grantedBy"),
        "since": lock.get("since"),
        "reason": lock.get("reason"),
        "ageMin": mins,
    }


def acquire(repo: Path, holder: str, role: str, reason: str, granted_by: str) -> None:
    data = {
        "holder": holder,
        "role": role,
        "reason": reason,
        "grantedBy": granted_by,
        "since": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    path = lock_path(repo)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # atomic, same reasoning as save_backlog/record_answer


def release(repo: Path) -> None:
    path = lock_path(repo)
    if path.exists():
        path.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Working-tree ownership lock for a status-board project."
    )
    ap.add_argument("--repo", required=True, help="absolute path to the project repo")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="print the current lock as JSON")

    p_acq = sub.add_parser("acquire", help="grant tree ownership")
    p_acq.add_argument(
        "--holder", required=True, help="who now holds it — a teammate/subagent name"
    )
    p_acq.add_argument(
        "--role", required=True, help="team-roles.md role, e.g. dev, claim-verifier, git-op"
    )
    p_acq.add_argument("--reason", default="", help="one line: what they're doing")
    p_acq.add_argument("--granted-by", default="team-lead", help="who granted it")

    sub.add_parser("release", help="clear the lock")

    args = ap.parse_args()
    repo = Path(args.repo).expanduser().resolve()

    if args.cmd == "status":
        print(json.dumps(status(repo), indent=2))
        return 0
    if args.cmd == "acquire":
        acquire(repo, args.holder, args.role, args.reason, args.granted_by)
        print(json.dumps(status(repo), indent=2))
        return 0
    if args.cmd == "release":
        release(repo)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
