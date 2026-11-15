#!/usr/bin/env python3
"""Is this session a worker that Orca dispatched? Shared by the session hooks.

Under Orca the main session is a coordinator, and it starts workers with
`orca orchestration worker-start`. A worker is a whole Claude Code session of
its own, so every SessionStart and UserPromptSubmit hook of this plugin fires
in it as well. Three of them were written for the owner's own session and do
the wrong thing in a worker's:

  * answer-waiter.py would claim the owner's answers in the worker and wake the
    worker with them, hiding them from the coordinator.
  * board-answers.py printed the owner's answers into the worker's context,
    told it to act on them, and marked them as surfaced, so the coordinator's
    own hook then stayed silent about the same answers.
  * offer-bootstrap.py offered /project-bootstrap before the worker's task,
    in a worktree that is one task's checkout, not a new project. It now offers
    only inside Orca, in a git project with no .claude/rules/operating-model.md
    (here or in its main checkout), and stays silent in a worker as before.

Nothing in the environment tells the two apart: a worker and a coordinator
are launched with the same flags and the same ORCA_* variables (compared on
12 Nov 2026, Orca 1.4.257: only HISTFILE and DISABLE_AUTOUPDATER differed).
What does tell them apart, in the order it is asked:

  1. The session's own first prompt. A worker's opens with Orca's preamble
     ("...terminal handle is: term_... Your task ID is: task_..."), and that
     prompt is the first user record of its transcript. This is evidence
     about this session and no other, so it is asked first.
  2. The prompt being submitted, but only when it is the session's first
     (the transcript holds no assistant turn yet). Later prompts are not
     trusted: an owner who pastes a worker's brief into the coordinator to
     ask about it is quoting a dispatch, not receiving one.
  3. Orca's own record: a dispatch that is still live on this session's
     terminal. It is the only evidence at SessionStart, when there is no
     prompt and no transcript yet, and it covers a worker whose transcript
     no longer begins with its dispatch.

Only a LIVE dispatch counts in 3. `worker-list` keeps settled workers, a
worker's terminal stays open after it reports, and Orca reuses terminals. An
earlier version matched any row, so the owner's own session, started in a
terminal that had once hosted a worker, was taken for a worker and heard
nothing: no waiter, no answers, no offer (review of 13 Nov 2026). A row
whose worker has settled, or whose process has exited, says who used to be
there.

Every path fails towards "not a worker": the hook then does what it did
before this file existed. A wrong yes silences the owner's own session, which
is the worse mistake.

The CLI is called the way the board calls it (status-board/orca_crew.py,
imported, not copied): a listing only, with the caller's identity removed. It
is given one second. A listing answers in a quarter of that, and a hook that
waits on a hung Orca delays every session start by as long as it waits.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping

DISPATCH = re.compile(
    r"terminal handle is: term_[0-9a-f-]{8,}.{0,200}?Your task ID is: task_[0-9a-f]{6,}",
    re.DOTALL,
)
HEAD_BYTES = 1_000_000  # the first prompt sits within the first megabyte
CLI_TIMEOUT_S = 1.0


def in_orca(env: Mapping[str, str] = os.environ) -> bool:
    return bool(env.get("ORCA_TERMINAL_HANDLE"))


def first_prompt_verdict(payload: dict) -> bool | None:
    """What the session's transcript says about its own first prompt.

    True: a user record before the first assistant turn is an Orca dispatch.
    False: the session has answered at least once, and nothing before that
    answer was a dispatch. None: there is no assistant turn yet, so the
    first prompt may be the one being submitted right now.
    """
    raw = payload.get("transcript_path")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        with Path(raw).expanduser().open("rb") as fh:
            head = fh.read(HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return None
    for line in head.split("\n"):
        if '"type":"assistant"' in line:
            return False
        if '"type":"user"' in line and DISPATCH.search(line):
            return True
    return None


def dispatched_by_prompt(payload: dict) -> bool:
    prompt = payload.get("prompt")
    return isinstance(prompt, str) and bool(DISPATCH.search(prompt))


def has_live_dispatch(env: Mapping[str, str] = os.environ) -> bool:
    """True when Orca lists a dispatch on this session's terminal that has not
    settled and whose process has not exited."""
    handle = env.get("ORCA_TERMINAL_HANDLE")
    if not handle:
        return False
    sys.dont_write_bytecode = True  # never write into the plugin directory
    board = str(Path(__file__).resolve().parent.parent / "status-board")
    if board not in sys.path:
        sys.path.insert(0, board)
    import orca_crew

    cli = orca_crew.resolve_cli(env)
    if cli is None:
        return False
    try:
        result = orca_crew.run_json(
            cli, orca_crew.READ_ONLY["workers"], orca_crew.cli_env(env), CLI_TIMEOUT_S
        )
    except orca_crew.OrcaError:
        return False
    return any(
        w["terminal"] == handle
        and w["state"] not in orca_crew.SETTLED
        and w["liveness"] != "exited"
        for w in orca_crew.parse_workers(result)
    )


def _normal(handle: str) -> str:
    """term_00000000-1111-2222-3333-444444444444 and term_00000000111122223333444444444444 name the same terminal."""
    return re.sub(r"^term_|-", "", handle.strip().lower())


def is_coordinator(env: Mapping[str, str] = os.environ) -> bool:
    """True when an Orca Run names this session's terminal as its coordinator.

    The keeper rule (14 Nov 2026) falls back on the Orca coordinator of the
    project when no writer is open. A Run's `coordinator_handle` equals the
    coordinator's own ORCA_TERMINAL_HANDLE, character for character (checked
    from a real coordinator, 14 Nov 2026); they are compared normalised anyway.
    The listing needs `--full` (without it a Run has no coordinator_handle) and
    is the one the board already asks (`orca_crew.READ_ONLY["runs"]`), with the
    caller's identity removed, one second at most. Outside Orca: False at once.
    Any doubt, any error: False, which only costs the fallback."""
    handle = env.get("ORCA_TERMINAL_HANDLE")
    if not handle:
        return False
    try:
        sys.dont_write_bytecode = True  # never write into the plugin directory
        board = str(Path(__file__).resolve().parent.parent / "status-board")
        if board not in sys.path:
            sys.path.insert(0, board)
        import orca_crew

        cli = orca_crew.resolve_cli(env)
        if cli is None:
            return False
        result = orca_crew.run_json(cli, orca_crew.READ_ONLY["runs"], orca_crew.cli_env(env), CLI_TIMEOUT_S)
        return any(r.get("coordinator") and _normal(r["coordinator"]) == _normal(handle)
                   for r in orca_crew.parse_runs(result).values())
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        return False


TEAMMATE_FLAGS = ("--agent-id", "--parent-session-id")


def teammate_parent(limit: int = 8) -> str | None:
    """Is an ancestor of this process a split-pane teammate? None when not;
    otherwise the session id of its lead (the value of `--parent-session-id`),
    or "" when only `--agent-id` is there. A split-pane teammate is a separate
    `claude` process, and recorded launches carry both flags (offer-bootstrap.py,
    31 Oct 2026; not seen in a live teammate again on 14 Nov 2026, so a doubt is
    "teammate": it is never told and its writes count for no one).

    `agent_type` alone is NOT a sign: a main session started with --agent has
    it. Moved here from offer-bootstrap.py, which asks `is not None`."""
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table: dict[int, tuple[int, list[str]]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2].split() if len(parts) > 2 else [])
    pid, seen = os.getppid(), 0
    while pid > 1 and pid in table and seen < limit:
        ppid, argv = table[pid]
        if any(t == f or t.startswith(f + "=") for t in argv for f in TEAMMATE_FLAGS):
            parent = ""
            for i, token in enumerate(argv):
                if token == "--parent-session-id" and i + 1 < len(argv):
                    parent = argv[i + 1]
                elif token.startswith("--parent-session-id="):
                    parent = token.split("=", 1)[1]
            return parent
        pid, seen = ppid, seen + 1
    return None


def is_worker(payload: dict, env: Mapping[str, str] = os.environ) -> bool:
    """True only on evidence. Any doubt, any error: False."""
    try:
        if not in_orca(env):
            return False
        verdict = first_prompt_verdict(payload)
        if verdict is True:
            return True
        if verdict is None and dispatched_by_prompt(payload):
            return True
        return has_live_dispatch(env)
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        return False
