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

import fcntl
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


def read_pid(lock_path: Path) -> int | None:
    """Best-effort read of the pid recorded in an existing lock file — for
    naming the incumbent in a refusal message, never for deciding whether
    it still holds the lock. That decision belongs to `acquire_lock`
    alone, via the kernel, not to a number read from disk."""
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def acquire_lock(lock_path: Path):
    """Exclusive, per-board watcher lock via `flock` — deliberately not a
    hand-rolled "read a pid, check it's alive" scheme.

    A pid file the watcher itself judges is exactly the trap this project
    keeps meeting in other costumes: pids are reused, so a stale record
    naming a pid that now belongs to an unrelated process would make this
    watcher inherit that process's identity and either refuse forever
    against a ghost or, worse, treat a live unrelated process as "our own
    old watcher, safe to ignore". `flock` sidesteps the question rather
    than answering it carefully: the lock is a property the kernel tracks
    against the open file descriptor itself, not a fact this script
    reconstructs from a number. A watcher killed with `kill -9` cannot
    clean up, but it also cannot keep holding a kernel-tracked lock after
    its process table entry is gone — release is automatic, with no
    staleness window and no identity check to get wrong.

    The pid is still written into the file's content on success, purely
    so a *refused* watcher has something to name in its own message
    ("already armed (pid N)") — that string is for a human reading the
    log, never an input to this function's own decision.

    Returns the open file object on success (kept open for the process's
    entire life — closing it releases the lock), or `None` if another
    live watcher already holds it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    fh = os.fdopen(fd, "r+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


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

    # One watcher per board, not one per session — a compacted session has
    # no memory of a watcher it already armed, so a hook telling it "arm
    # unless already armed" is instructing a reader who lost the only
    # record that would let it comply. This is the actual fix: something
    # that can observe the truth (a live process holding the lock)
    # instead of something that has to remember it.
    lock_path = answers_file.parent / "watcher.lock"
    previous_pid = read_pid(lock_path)
    lock_fh = acquire_lock(lock_path)
    if lock_fh is None:
        incumbent = read_pid(lock_path)
        who = f"pid {incumbent}" if incumbent is not None else "an unknown process"
        # Deliberately does not contain the substring "WATCHER ARMED" —
        # the caller tells armed from not-armed by looking for that exact
        # line, and a refusal that could be mistaken for one would be a
        # new silent failure wearing the old one's clothes.
        print(
            f"WATCHER REFUSED — {who} is already armed on {answers_file}; "
            "not starting a second watcher against the same board. The "
            "existing one is untouched and still running.",
            flush=True,
        )
        return 1

    # Distinguish a fresh start from taking over a lock a killed watcher
    # left behind — both are correct, but only one of them is worth a
    # human noticing, and folding them into one silent case would hide
    # exactly the recovery this lock exists to make possible.
    if previous_pid is not None and previous_pid != os.getpid():
        print(
            f"WATCHER ARMED on {answers_file} — took over a lock left behind "
            f"by pid {previous_pid} (no longer running), primed with "
            f"{len(seen)} existing answer(s), none will be replayed",
            flush=True,
        )
    else:
        # Always announce. Without this line a dead watcher and a quiet
        # watcher are the same observation — the "silence is not success"
        # trap, reintroduced by the very guard meant to fix it.
        print(
            f"WATCHER ARMED on {answers_file} — primed with {len(seen)} existing "
            "answer(s), none will be replayed",
            flush=True,
        )

    try:
        while True:
            time.sleep(INTERVAL)
            current = load(answers_file)
            if current is None:
                continue
            for question, record in current.items():
                # Keyed on question -> (timestamp, answer), so re-answering
                # the same question surfaces again instead of being
                # suppressed as seen — including when the owner changes
                # their mind and the answer text differs while the
                # question is identical.
                if seen.get(question) != record:
                    seen[question] = record
                    at, answer = record
                    # Both halves, on one line each, so the notification is
                    # self-sufficient. Stdout lines within 200ms batch into
                    # a single notification, so this stays one event.
                    print(f"BOARD ANSWER — {question}", flush=True)
                    print(f"  → {answer}   [{at}]", flush=True)
    finally:
        # Best-effort tidiness, not correctness: the kernel already
        # released the lock the moment our fd closes, whether that
        # happens here or via SIGKILL skipping this entirely. Only remove
        # the file if it still names us — a fresh watcher should never
        # have its own lock file deleted out from under it.
        try:
            if read_pid(lock_path) == os.getpid():
                lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        lock_fh.close()
    # No `return` here: the loop above never exits on its own, so the only
    # ways out of this function are the early `return 1`s above and
    # `KeyboardInterrupt` propagating past the `finally` — same shape as
    # before this change, just with cleanup now sitting in its path.


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
