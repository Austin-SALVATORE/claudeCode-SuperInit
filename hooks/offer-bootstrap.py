#!/usr/bin/env python3
"""Offer /project-bootstrap when a session starts in a folder with no team structure.

Before this, a fresh session in a new folder never suggested the bootstrap, so
the working practice this plugin exists to provide arrived only if the owner
remembered to ask for it.

Like arm-watcher.py this is an instruction, not a mechanism: a hook cannot run
a skill, only tell the agent to offer one. What the instruction insists on is
the order — ask what the project is and who works on it, get a yes, only then
scaffold — because a rule nobody follows is worse than no rule
(project-bootstrap/SKILL.md says the same of adapting the templates).

"No team structure" means no `.claude/rules/team-roles.md` at the cwd or at the
cwd's git top-level. A bare `.claude/` does not count: Claude Code creates
`.claude/settings.local.json` by itself, so testing for the directory would
treat almost every folder as already bootstrapped.

Silent when:
  * `source` is not `startup` or `clear` — resume, compact and fork continue a
    conversation that already had its chance, and offering again is nagging;
  * the cwd is $HOME, `/`, or anywhere inside ~/.claude;
  * the owner opted out: PROJECT_INIT_NO_OFFER=1 (everywhere), or an empty
    `.claude/no-bootstrap-offer` at the cwd or its git top-level (that folder);
  * the session is an agent rather than the owner's own (below).

Telling a teammate apart (looked into 31 Oct 2026, Claude Code 2.1.309-2.1.316):
  * Subagents never get here: five subagent transcripts hold SubagentStart and
    PreToolUse records and no SessionStart, while the lead's holds eleven. The
    docs run in-process teammates inside the lead's own process.
  * A split-pane teammate is a separate Claude Code process, and SessionStart
    does fire in it — this plugin's own arm-watcher.py ran in one, at
    `SessionStart:startup`. The documented payload has no teammate field, but
    `agent_type` is present when a session is started with an agent, and all six
    pane teammates examined were (each transcript opens with an `agent-setting`
    record). So a payload carrying agent_type or agent_id counts as an agent.
  * Backstop: cmux's session registry holds the recorded launch argv of seven
    real pane teammates, and every one carries `--agent-id` and
    `--parent-session-id`; the lead processes inspected carry neither. So an
    ancestor process with either flag on its command line marks a teammate.
  Neither tell has been seen in a live teammate's SessionStart: the flags come
  from recorded launches, agent_type from the docs and the agent-setting
  records, and both are exercised here only with synthetic input. If both are
  missing a teammate sees the offer: noise, not damage.

Fails quiet. Any error, unreadable input or unexpected state prints nothing and
exits 0 — the cost of a bug here is the status quo, no offer. A hook that
blocks the session it is greeting is worse than the gap it fills.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TEAM_ROLES = Path(".claude") / "rules" / "team-roles.md"
OPT_OUT_FILE = Path(".claude") / "no-bootstrap-offer"
OPT_OUT_ENV = "PROJECT_INIT_NO_OFFER"

OFFER_ON = {"startup", "clear"}

# Keys that identify an agent in hook payloads, never the owner's own session.
AGENT_KEYS = ("agent_type", "agent_id", "teammate_name", "team_name")

# On the command line of every split-pane teammate Claude Code launches.
TEAMMATE_FLAGS = ("--agent-id", "--parent-session-id")


def read_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}  # run by hand: there is no payload, so there is nothing to decide
    try:
        data = json.loads(sys.stdin.read(1_000_000))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def opted_out_by_env() -> bool:
    return os.environ.get(OPT_OUT_ENV, "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def excluded_location(cwd: Path, home: Path) -> bool:
    """$HOME, `/` and ~/.claude are never a project, so never offered one."""
    claude = home / ".claude"
    return (
        cwd == home
        or cwd == Path(cwd.anchor)
        or cwd == claude
        or claude in cwd.parents
    )


def git_toplevel(start: Path) -> Path | None:
    """Nearest ancestor holding a `.git` (a directory, or a file for worktrees
    and submodules). Walked in Python rather than asking git: it cannot be
    slowed by a hung repo or refused by a safe.directory check."""
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def project_roots(cwd: Path, home: Path) -> list[Path]:
    roots = [cwd]
    top = git_toplevel(cwd)
    # A dotfiles repo makes $HOME a git top-level; that is not this project.
    if top is not None and top not in (cwd, home, Path(top.anchor)):
        roots.append(top)
    return roots


def teammate_launched(limit: int = 8) -> bool:
    """True when an ancestor process was started with a teammate flag."""
    try:
        out = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    table: dict[int, tuple[int, list[str]]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2].split() if len(parts) > 2 else [])
    pid, seen = os.getppid(), 0
    while pid > 1 and pid in table and seen < limit:
        ppid, argv = table[pid]
        if any(t == f or t.startswith(f + "=") for t in argv for f in TEAMMATE_FLAGS):
            return True
        pid, seen = ppid, seen + 1
    return False


def agent_session(payload: dict) -> bool:
    return any(payload.get(k) for k in AGENT_KEYS) or teammate_launched()


def main() -> int:
    payload = read_payload()
    if payload.get("source") not in OFFER_ON or opted_out_by_env():
        return 0
    raw = payload.get("cwd")
    if not isinstance(raw, str) or not raw:
        return 0
    cwd, home = Path(raw).resolve(), Path.home().resolve()
    if not cwd.is_dir() or excluded_location(cwd, home):
        return 0
    roots = project_roots(cwd, home)
    if any((r / TEAM_ROLES).is_file() or (r / OPT_OUT_FILE).exists() for r in roots):
        return 0
    if agent_session(payload):  # last: it costs a `ps`
        return 0

    print("<bootstrap-offer>")
    print(
        f"This folder ({cwd}) has no .claude/ team structure: no "
        ".claude/rules/team-roles.md at the cwd or its git top-level. Before "
        "other work, offer the owner /project-bootstrap in a sentence or two. "
        "Ask what the project is and who works on it first, and do not create "
        "or change any file until the owner says yes. If they decline, carry on "
        "and do not raise it again this session."
    )
    print(
        "To stop this offer: create an empty .claude/no-bootstrap-offer in this "
        "folder (only if the owner asks you to), or set PROJECT_INIT_NO_OFFER=1 "
        "to stop it everywhere."
    )
    print("</bootstrap-offer>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
