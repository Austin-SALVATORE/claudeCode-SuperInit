#!/usr/bin/env python3
"""Emit one line per newly-tapped board answer, as it happens.

Run under the agent's Monitor tool: every stdout line becomes a notification,
so an answer reaches the agent seconds after the tap instead of waiting for
the owner's next message.

WHY THIS EXISTS, AND WHY THE HOOK WAS NOT ENOUGH
------------------------------------------------
`hooks/board-answers.py` already surfaces answers — but only on
`UserPromptSubmit`, so a tap cannot reach the agent until the owner speaks
again. That was diagnosed once as a structural property of the system, and
the diagnosis was wrong: it described the limit of *one instrument* and
concluded it was the limit of the world. A background process re-invokes the
agent when it exits, and a monitor delivers each stdout line as a
notification while the agent is idle. Both were available the whole time.

The tell was that every check performed on the hook passed — it was
registered, the plugin hook count rose, it fired on the next message — and
none of those checks could have detected the actual problem. That is the
"change instrument, not effort" rule in `rules/verification.md`, failed in
the wild by the person who wrote it down.

KEEP BOTH. They fail differently:
  * the hook is durable (plugin config, survives every session) and slow
  * this watcher is fast and dies with the session that armed it
Together, a tap is never lost even when one of them is dead.

USAGE
  python3 watch-answers.py [REPO_PATH]      # defaults to $PWD

Env:
  STATUS_BOARD_HOME  board state root (default ~/.claude/status-board)
  INTERVAL           poll seconds (default 2)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

STATE = Path(
    os.environ.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board"
).expanduser()
INTERVAL = float(os.environ.get("INTERVAL", "2"))


def slug_for(repo: Path) -> str:
    # Must match serve.py and board-answers.py exactly, or this watches a
    # board that does not exist and stays silent forever — which is
    # indistinguishable from a board nobody has tapped.
    return repo.name.lower().replace(" ", "-")


def load(path: Path):
    """{question: (at, answer)}, or None if unreadable *right now*.

    The answer is carried alongside the timestamp on purpose. An earlier
    version tracked only `at` and emitted the question alone, which made
    every notification say *that* a decision had been made without saying
    *what* — so the agent still had to wait for the owner's next message to
    learn the content, and the slow path it was built to replace was doing
    all the real work. A notification that cannot be acted on is not a
    notification, it is a reminder to go and look.

    Unreadable is routine, not exceptional: the server rewrites this file
    non-atomically, so a poll can land mid-write. Returning None makes the
    caller skip the round. Returning {} instead would mark every answer as
    new on the following read and replay the entire history.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        return {
            q: (a.get("at"), a.get("answer"))
            for q, a in data.items()
            if isinstance(a, dict)
        }
    except (OSError, ValueError):
        return None


def main() -> int:
    repo = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.cwd()
    answers_file = STATE / "boards" / slug_for(repo) / "answers.json"

    seen = load(answers_file)
    if seen is None:
        # Fail loudly at startup rather than watching nothing in silence. A
        # wrong path and an untapped board look identical from outside, and
        # silence is the exact failure this file exists to remove.
        print(
            f"WATCHER ERROR — cannot read {answers_file} at startup; not watching. "
            "Check the board slug and STATUS_BOARD_HOME.",
            flush=True,
        )
        return 1

    # Always announce. Without this line a dead watcher and a quiet watcher
    # are the same observation — the "silence is not success" trap,
    # reintroduced by the very guard meant to fix it.
    print(
        f"WATCHER ARMED on {answers_file} — primed with {len(seen)} existing "
        "answer(s), none will be replayed",
        flush=True,
    )

    while True:
        time.sleep(INTERVAL)
        current = load(answers_file)
        if current is None:
            continue
        for question, record in current.items():
            # Keyed on question -> (timestamp, answer), so re-answering the
            # same question surfaces again instead of being suppressed as
            # seen — including when the owner changes their mind and the
            # answer text differs while the question is identical.
            if seen.get(question) != record:
                seen[question] = record
                at, answer = record
                # Both halves, on one line each, so the notification is
                # self-sufficient. Stdout lines within 200ms batch into a
                # single notification, so this stays one event.
                print(f"BOARD ANSWER — {question}", flush=True)
                print(f"  → {answer}   [{at}]", flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
