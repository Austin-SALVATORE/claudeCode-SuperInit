#!/usr/bin/env python3
"""Surface newly-tapped status-board answers into the agent's context.

The board records an answer server-side the instant the owner taps, and
*nothing notifies the agent*. The skill tells the agent to read answers.json
before every reply; that instruction was broken within an hour of being
written, which is the whole argument for this file. A rule the agent has to
remember is not a mechanism. This is the mechanism.

Runs on UserPromptSubmit. Anything printed to stdout is added to the agent's
context for that turn. Prints nothing when there is nothing new, so a quiet
board costs a few milliseconds and no tokens.

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
import os
import sys
from pathlib import Path

STATE = Path(
    os.environ.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board"
).expanduser()


def slug_for(repo: Path) -> str:
    # Must match serve.py's slug_for exactly, or the hook reads a board that
    # does not exist and silently reports nothing forever.
    return repo.name.lower().replace(" ", "-")


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

    board = STATE / "boards" / slug_for(cwd)
    answers_file = board / "answers.json"
    if not answers_file.is_file():
        return 0

    try:
        answers = json.loads(answers_file.read_text())
    except Exception:
        return 0
    if not isinstance(answers, dict) or not answers:
        return 0

    # Seen-marker keyed by question -> timestamp, so re-answering a question
    # surfaces again rather than being suppressed as "already seen".
    seen_file = board / ".surfaced-to-agent.json"
    try:
        seen = json.loads(seen_file.read_text())
        if not isinstance(seen, dict):
            seen = {}
    except Exception:
        seen = {}

    fresh = [
        (q, a)
        for q, a in answers.items()
        if isinstance(a, dict) and seen.get(q) != a.get("at")
    ]
    if not fresh:
        return 0
    # Asked last, and only when there is something to say: the answer may cost
    # a call to Orca, and a quiet board must stay free.
    if orca_worker(payload):
        return 0

    fresh.sort(key=lambda qa: qa[1].get("at") or "")

    print("<board-answers>")
    print(
        f"The owner answered {len(fresh)} question(s) on the status board. "
        "These are recorded rulings, not suggestions: the recommended option "
        "records on a single tap, and any other option required an explicit "
        "confirmation in a modal. Act on them, move them to `answered` in "
        "narrative.json, and do not ask again."
    )
    for q, a in fresh:
        print(f"\n  Q: {q}")
        print(f"  A: {a.get('answer')}   [{a.get('at')}]")
    print("</board-answers>")

    try:
        seen.update({q: a.get("at") for q, a in fresh})
        seen_file.write_text(json.dumps(seen, indent=2, ensure_ascii=False))
    except Exception:
        # Failing to persist means repeating ourselves next turn, which is
        # noisy but harmless. Losing the answer entirely is not.
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
