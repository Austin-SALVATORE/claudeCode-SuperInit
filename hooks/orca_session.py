#!/usr/bin/env python3
"""Is this session a worker that Orca dispatched? Shared by the session hooks.

Under Orca the main session is a coordinator, and it starts workers with
`orca orchestration worker-start`. A worker is a whole Claude Code session of
its own, so every SessionStart and UserPromptSubmit hook of this plugin fires
in it as well. Three of them were written for the owner's own session and do
the wrong thing in a worker's:

  * arm-watcher.py told the worker to arm the answer watcher. There is one
    watcher per board (a kernel lock), so a worker that armed first took it
    from the coordinator, and the owner's answers went to a session that was
    never meant to act on them and that closes when its task ends.
  * board-answers.py printed the owner's answers into the worker's context,
    told it to act on them, and marked them as surfaced, so the coordinator's
    own hook then stayed silent about the same answers.
  * offer-bootstrap.py offered /project-bootstrap before the worker's task,
    in a worktree that is one task's checkout, not a new project.

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
nothing: no watcher, no answers, no offer (review of 13 Nov 2026). A row
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
