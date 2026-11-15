#!/usr/bin/env python3
"""Surface newly-tapped status-board answers into the agent's context.

The board records an answer server-side the instant the owner taps, and
nothing in the page tells the agent. The skill tells the agent to read answers.json
before it asks anything; that instruction was broken within an hour of being
written, which is the whole argument for this file. A rule the agent has to
remember is not a mechanism. This is the mechanism.

Runs on UserPromptSubmit. Anything printed to stdout is added to the agent's
context for that turn. Prints nothing when there is nothing new, so a quiet
board costs a few milliseconds and no tokens.

This is the slow path: it can only fire when a prompt is submitted. The fast
path is hooks/answer-waiter.py, which wakes the session within seconds of a
tap. Both take their answers from status-board/answer_ledger.py, which marks
what it hands out under one lock, so an answer is told once, whichever of the
two got to it first. Since 14 Nov 2026 the marker is no longer read and
rewritten here with no lock, which let two writers lose each other's entries.

ONLY THE SESSION THAT LAST WROTE THE BOARD IS TOLD (the owner's ruling, 14 Nov
2026), here as in the waiter. In any other session of the folder this hook
claims nothing and prints, once per change of keeper and only when an answer is
waiting that this session is not given, one `<board-keeper>` line saying who
keeps the board and how to take it over. When that session is open but no answer
waiter runs in it (it was idle past the waiter's 21 days, or its window was left
open and forgotten), the line says instead, once per answer and never with the
answer, that one waits unheard, so that the owner is told (review finding 4; the
ruling itself stays strict). A project whose folder name's board belongs to another
project that still exists is told once, in a `<board-elsewhere>` block, whose board it
is and how to get its own; a board whose recorded project no longer exists is taken
over by the project that asks (finding 1). A session the owner types into counts as
the most recently active at that moment, so when the writer's session is closed
the answer goes to the session he is typing in. A worker, a split-pane teammate,
a subagent and a headless `claude -p` run are never told, and this hook is silent
in them and leaves the marker alone.

It also notices when the fast path is not running (no waiter registered for
this session's `claude`, or one whose process is gone) and says so to the agent,
once: that is how a Claude Code that stops honouring `asyncRewake`, or a waiter
that crashed, shows up in the session instead of as answers that are merely
late. And it says, once, when the record of what was already told had to be
rebuilt, which answers it counted as told.

Never fails the prompt: every path exits 0, and any unexpected error is
swallowed. A broken reporting hook must not be able to block the session it
reports on.

Silent in a worker that Orca dispatched, and it leaves the seen-marker alone
there. A worker started in the project's checkout gets each task as a prompt,
so this hook fires in it; before this rule it printed the owner's answers to
the worker, told it to act on them, and marked them surfaced, which silenced
the same answers in the coordinator's session, the one they were for.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # never write into the plugin directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "status-board"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def orca_worker(payload: dict) -> bool:
    """True when this session is a worker that Orca dispatched (see
    orca_session.py). Imported from beside this file; any failure means no."""
    try:
        import orca_session

        return orca_session.is_worker(payload)
    except Exception:
        return False


def never_told(payload: dict, command: str | None, ledger) -> bool:
    """A subagent or in-process teammate (its payload has an agent_id), a
    split-pane teammate (a process launched with the teammate flags) or a
    headless `claude -p` run. When in doubt about a teammate: it is one."""
    if payload.get("agent_id") or payload.get("teammate_name") or ledger.is_headless(command):
        return True
    try:
        import orca_session

        return orca_session.teammate_parent() is not None
    except Exception:
        return False


def main() -> int:
    cwd = Path.cwd()
    payload: dict = {}
    try:
        loaded = json.load(sys.stdin)
        if isinstance(loaded, dict):
            payload = loaded
            if payload.get("cwd"):
                cwd = Path(payload["cwd"])
    except Exception:
        pass  # no stdin, or not JSON — cwd is a fine fallback

    import answer_ledger as ledger

    board = ledger.board_for(cwd)
    if board is None:
        # Another project of the same folder name holds this folder name's board: never silent (the review of
        # 14 Nov 2026, finding 1). Said once per session, and only to a session that could be told anything.
        refused = ledger.refused_board(cwd)
        if refused is not None and refused.is_dir():
            anchor = ledger.find_anchor()
            if not never_told(payload, ledger.anchor_command(anchor) if anchor else None, ledger) and not orca_worker(payload):
                said = ledger.elsewhere_note(cwd, anchor, payload.get("session_id"), refused)
                if said:
                    sys.stdout.write(said)
        return 0
    if not board.is_dir():
        return 0  # not a board project: nothing to say, nothing written

    anchor = ledger.find_anchor()
    session_id = payload.get("session_id")
    if never_told(payload, ledger.anchor_command(anchor) if anchor else None, ledger):
        return 0
    fresh_possible = ledger.peek_fresh(board)
    note = ledger.waiter_note(board, anchor, session_id)
    own = ledger.read_registration(board, anchor) or {}
    me = {"session_id": session_id, "anchor": anchor, "started": time.time(),
          "orca_role": own.get("orca_role") if own.get("session_id") == session_id else None}
    keeper_here = ledger.is_keeper(board, session_id, anchor, me=me)
    # A session that is not the keeper is told once that an answer landed which it was not given: nothing may be
    # waiting by then (the keeper's waiter claims within seconds), so "landed since this session began" counts.
    landed = fresh_possible or (not keeper_here and ledger.answered_since(board, ledger.session_start(payload)))
    if not landed and note is None and not ledger.seed_note_due(board):
        return 0
    # Asked last, and only when there is something to say: the answer may cost
    # a call to Orca, and a quiet board must stay free. Before anything is
    # claimed, because claiming marks the answers told.
    if orca_worker(payload):
        return 0

    out = ""
    if keeper_here:
        fresh = ledger.claim(board, ledger.session_start(payload), lambda: ledger.is_keeper(board, session_id, anchor, me=me))
        out += ledger.format_block(fresh) if fresh else ""
        out += ledger.take_seed_note(board) or ""
        if note is not None:
            out += note
            ledger.mark_reported(board, anchor, session_id)
    elif landed:
        keeper = ledger.keeper_for(board, me=me)
        unheard = ledger.unheard_note(board, anchor, session_id, keeper)
        if unheard is not None:
            # The keeper is open but no waiter runs in it (idle for days, or forgotten): its answer waits unheard.
            # Said once per answer, never with the answer; the plain line would say the opposite ("need not do anything").
            said, ats = unheard
            if said:
                out += said
                ledger.mark_unheard_noted(board, anchor, session_id, ats)
        else:
            said = ledger.keeper_note(board, anchor, session_id, keeper)
            if said:
                out += said
                ledger.mark_keeper_noted(board, anchor, session_id, keeper)
    if out:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code)
