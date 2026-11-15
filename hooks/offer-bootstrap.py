#!/usr/bin/env python3
"""Offer /project-bootstrap, inside Orca, in a git project that has no operating model.

Before this, a fresh session in a new folder never suggested the bootstrap, so
the working practice this plugin exists to provide arrived only if the owner
remembered to ask for it.

This is an instruction, not a mechanism: a hook cannot run a skill, only tell
the agent to offer one. What the instruction insists on is
the order — ask what the project is, get a yes, only then scaffold — because a
rule nobody follows is worse than no rule (project-bootstrap/SKILL.md says the
same of adapting the templates).

What the bootstrap writes (15 Nov 2026, release 2.2.0) is how a project is run
under Orca and oh-my-claudecode: `.claude/rules/operating-model.md` and the files
beside it. It names Orca's commands, so the offer is made only where they exist.
It used to write a team of Claude Code agents, and `team-roles.md` was the file
this hook looked for; it still recognises that older set (below).

"No operating model" means no `.claude/rules/operating-model.md` at the cwd, at
the cwd's git top-level, or in the project's main checkout. A bare `.claude/` does
not count: Claude Code creates `.claude/settings.local.json` by itself, so testing
for the directory would treat almost every folder as already bootstrapped.

The main checkout is found without git, from the `.git` file of a linked worktree
(`gitdir: <common>/worktrees/<name>`, then the `commondir` file inside it, whose
parent is the main checkout). Orca gives each writing worker a worktree of its
own, and a scaffold that is not yet committed on that branch is only in the main
checkout: without this, the worktree of a bootstrapped project would be offered
the bootstrap.

Offers when all of these hold:
  * `source` is `startup` or `clear` — resume, compact and fork continue a
    conversation that already had its chance, and offering again is nagging;
  * the owner has not opted out: PROJECT_INIT_NO_OFFER=1 (everywhere), or an empty
    `.claude/no-bootstrap-offer` at the cwd, its git top-level or its main
    checkout (that project);
  * the session is inside Orca (ORCA_TERMINAL_HANDLE is set, as in
    orca_session.in_orca): the owner chose, on 15 Nov 2026, that outside Orca a
    new folder gets no offer, because the scaffold is about Orca's commands;
  * the cwd is in a git project: a folder with no `.git` above it, $HOME (a
    dotfiles repository makes it a git top-level), `/` and anywhere inside
    ~/.claude are not projects;
  * the session is the owner's own, not an agent's (below);
  * a person is there to answer: not `claude -p` or the SDK, where Claude Code
    sets CLAUDE_CODE_SESSION_ATTENDED=0 and a CLAUDE_CODE_ENTRYPOINT that starts
    with `sdk` (15 Nov 2026: such a run, started inside an Orca terminal, was
    handed the offer with nobody to answer it). Checked first; with both
    variables missing, as in an older Claude Code, the session counts as attended;
  * the project has no operating model (above).

A project with the older set only (`.claude/rules/team-roles.md` and no
operating-model.md) gets a `<bootstrap-update>` note instead of the offer, on the
same conditions: one sentence to the owner that `/project-bootstrap update` can
show what it would add and retire, and that the old set's tree lock no longer
runs (until the update, one writing agent at a time). Nothing here ever edits a
repository.

Telling a teammate apart (looked into 31 Oct 2026, Claude Code 2.1.309-2.1.316):
  * Subagents never get here: five subagent transcripts hold SubagentStart and
    PreToolUse records and no SessionStart, while the lead's holds eleven. The
    docs run in-process teammates inside the lead's own process.
  * A split-pane teammate is a separate Claude Code process, and SessionStart
    does fire in it — this plugin's own answer-waiter.py runs in one, at
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

An Orca worker is a third kind of agent session, and none of the above sees
it (12 Nov 2026, Orca 1.4.257): it is a plain `claude` process with no agent
flag and no agent_type, started in a checkout that is often a fresh worktree
with no `.claude/` at all. It got the offer, ahead of the task it was
dispatched for. `orca_session.py` recognises it from Orca's own record.

Fails quiet. Any error, unreadable input or unexpected state prints nothing and
exits 0 — the cost of a bug here is the status quo, no offer. A hook that
blocks the session it is greeting is worse than the gap it fills. It writes
nothing, asks no network, and is quick: two calls can wait, both in
orca_session.py: `ps` for the teammate walk (2 s at most) and Orca's own listing
(1 s).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

OPERATING_MODEL = Path(".claude") / "rules" / "operating-model.md"
OLD_TEAM_ROLES = Path(".claude") / "rules" / "team-roles.md"  # the set written before 2.2.0
OPT_OUT_FILE = Path(".claude") / "no-bootstrap-offer"
OPT_OUT_ENV = "PROJECT_INIT_NO_OFFER"
ORCA_HANDLE_ENV = "ORCA_TERMINAL_HANDLE"  # orca_session.in_orca asks the same variable
# What Claude Code tells its hooks about a run (measured 15 Nov 2026, Claude Code 2.1.325): `claude -p` had
# CLAUDE_CODE_SESSION_ATTENDED=0 and CLAUDE_CODE_ENTRYPOINT=sdk-cli, an interactive session 1 and cli.
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"
ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"
COMMONDIR_MAX_BYTES = 4096  # a worktree's `commondir` holds a short relative path

OFFER_ON = {"startup", "clear"}

# Keys that identify an agent in hook payloads, never the owner's own session.
AGENT_KEYS = ("agent_type", "agent_id", "teammate_name", "team_name")

# The flags on the command line of every split-pane teammate: orca_session.TEAMMATE_FLAGS.


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


def unattended() -> bool:
    """True in a run nobody is there to answer: `claude -p` and the SDK. Either
    variable is enough; with both missing (an older Claude Code) the session is
    taken for an owner's, as before."""
    return (
        os.environ.get(ATTENDED_ENV, "").strip() == "0"
        or os.environ.get(ENTRYPOINT_ENV, "").strip().startswith("sdk")
    )


def inside_orca() -> bool:
    """True in a terminal Orca opened. Asked of the environment rather than of
    orca_session, which says the same thing in one line: a session outside Orca,
    the common one, then imports nothing and stops here."""
    return bool(os.environ.get(ORCA_HANDLE_ENV))


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


def main_checkout(top: Path) -> Path | None:
    """The main checkout of the repository that `top` is a linked worktree of, or
    None. Read from files, not from git: `top/.git` is a file `gitdir: <path>`
    for a linked worktree (a directory for a main checkout, which has no other),
    that folder holds a `commondir` file naming the repository's own `.git`, and
    the parent of that is the main checkout. A submodule's `.git` file has no
    `commondir` beside it, and a bare repository's common dir is not named
    `.git`: both give None, and the project is then only the folder itself."""
    try:
        dot_git = top / ".git"
        if not dot_git.is_file():
            return None
        text = dot_git.read_text(errors="replace")[:4096]
        if not text.startswith("gitdir:"):
            return None
        gitdir = Path(text[len("gitdir:") :].strip())
        if not gitdir.is_absolute():
            gitdir = top / gitdir
        commondir = gitdir / "commondir"
        info = commondir.stat()  # stat does not block; a read of a FIFO does, until Claude Code ends the hook
        if not stat.S_ISREG(info.st_mode) or info.st_size > COMMONDIR_MAX_BYTES:
            return None
        common = (gitdir / commondir.read_text(errors="replace").strip()).resolve()
        main = common.parent
        return main if common.name == ".git" and main != top and main.is_dir() else None
    except (OSError, ValueError):
        return None


def project_roots(cwd: Path, home: Path) -> tuple[Path | None, list[Path]]:
    """(the project's root, the folders that can hold its scaffold or its opt-out).

    The root is the cwd's git top-level, and there is none when the cwd is not in
    a git project. The folders are the cwd, the root and, for a worktree, its
    main checkout."""
    top = git_toplevel(cwd)
    # A dotfiles repository makes $HOME a git top-level; that is not this project.
    if top is None or top in (home, Path(top.anchor)):
        return None, []
    roots = [cwd, top]
    main = main_checkout(top)
    if main is not None:
        roots.append(main)
    return top, list(dict.fromkeys(roots))


def teammate_launched(limit: int = 8) -> bool:
    """True when an ancestor process was started with a teammate flag. The walk
    is `orca_session.teammate_parent` (moved there on 14 Nov 2026: the answer
    keeper needs the lead's session id from the same flags)."""
    try:
        sys.dont_write_bytecode = True  # never write into the plugin directory
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import orca_session

        return orca_session.teammate_parent(limit) is not None
    except Exception:
        return False


def agent_session(payload: dict) -> bool:
    return (
        any(payload.get(k) for k in AGENT_KEYS)
        or teammate_launched()
        or orca_worker(payload)
    )


def orca_worker(payload: dict) -> bool:
    """True when this session is a worker that Orca dispatched (see
    orca_session.py). Imported from beside this file; any failure means no."""
    try:
        sys.dont_write_bytecode = True  # never write into the plugin directory
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import orca_session

        return orca_session.is_worker(payload)
    except Exception:
        return False


def main() -> int:
    payload = read_payload()
    if unattended():  # first: there is nobody to ask, whatever else is true
        return 0
    if payload.get("source") not in OFFER_ON or opted_out_by_env():
        return 0
    raw = payload.get("cwd")
    if not isinstance(raw, str) or not raw:
        return 0
    cwd, home = Path(raw).resolve(), Path.home().resolve()
    if not cwd.is_dir() or excluded_location(cwd, home):
        return 0
    if not inside_orca():
        return 0
    root, roots = project_roots(cwd, home)
    if root is None or any((r / OPT_OUT_FILE).exists() for r in roots):
        return 0
    if any((r / OPERATING_MODEL).is_file() for r in roots):
        return 0
    if agent_session(payload):  # last: it costs a `ps`
        return 0

    if any((r / OLD_TEAM_ROLES).is_file() for r in roots):
        print("<bootstrap-update>")
        print(
            f"This project ({root}) has the older project-bootstrap set "
            "(.claude/rules/team-roles.md), written for Claude Code agent teams. "
            "The plugin now writes an Orca-only set. Tell the owner in one "
            "sentence, once, that /project-bootstrap update can show what it "
            "would add and retire, and that the old set's tree lock no longer "
            "runs, so until the update it is one writing agent at a time. Do "
            "not edit those files yourself."
        )
        print(
            "To stop this note: an empty .claude/no-bootstrap-offer in this "
            "project, or PROJECT_INIT_NO_OFFER=1."
        )
        print("</bootstrap-update>")
        return 0

    print("<bootstrap-offer>")
    print(
        f"This project ({root}) has no Orca operating model: no "
        ".claude/rules/operating-model.md here or in its main checkout. Before "
        "other work, offer the owner /project-bootstrap in one or two "
        "sentences: it writes a few files on how this project is run under "
        "Orca (the worker roster, the Task title convention, when an "
        "independent check is required, the workstream register). Ask what the "
        "project is first, and create or change no file until the owner says "
        "yes. If they decline, carry on and do not raise it again this session."
    )
    print(
        "To stop this offer: an empty .claude/no-bootstrap-offer in this "
        "project (only if the owner asks you to), or PROJECT_INIT_NO_OFFER=1 "
        "everywhere."
    )
    print("</bootstrap-offer>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
