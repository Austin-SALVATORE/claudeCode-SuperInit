#!/usr/bin/env python3
"""Wait in the background for a new status-board answer, and wake the session.

Claude Code starts this on SessionStart, UserPromptSubmit and Stop with
`"async": true, "asyncRewake": true`. It exits 2, with the answers on stderr,
when a new answer lands; Claude Code then starts a turn by itself and shows the
agent the text as a system reminder, even when the session is idle. Measured
14 Nov 2026 (Claude Code 2.1.325, interactive): exit 2 to a new turn in 0.06 s,
the agent's quote at about 1.9 s. Landing in the middle of a turn, the message
is queued and arrives when the running tool returns, once, inside the same
turn; no second turn starts.

WHY A HOOK, NOT A WATCHER THE AGENT ARMS
The watcher this replaces ran under the agent's Monitor tool. It was an
instruction, not a mechanism, and it decayed three ways: the instruction named
an option (a "persistent" flag) the Monitor tool does not have; a monitor ends
after 30 minutes at most, so the session was woken to re-arm it; and a running
monitor is a background task, which makes `/restart` refuse ("Can't restart
while work is running in the background"). An async hook is not that kind of
task: `/restart` went through with this kind of hook waiting (spike S3). And
Claude Code starts this again after every turn, so nothing rests on the agent.

WHO IS TOLD: THE SESSION THAT LAST WROTE THE BOARD
Every session in the project's folder runs this, and every one of them used to
claim whatever it polled first: with a lead and a teammate on one board the lead
was told 3 answers of 10 (the review, 14 Nov 2026). The owner ruled that only
the session that last wrote the board's content is told, the others stay silent,
and a new session that takes the board over becomes the one that is told. Who
that is, is decided in answer_ledger (`keeper_for`, from keeper.json and the
waiters' registrations, inside claim's lock); here a waiter that is not the
keeper claims nothing and keeps waiting, because the keeper can change, which
changes no file: it asks again every few seconds. Workers, split-pane teammates
and headless `claude -p` runs are never told: such a waiter writes its
registration as `role: worker|teammate|headless`, ended, and exits 0. Writing
that registration (F2 of the review) is also what stops an older waiter of the
same claude: a worker that Orca's listing missed at SessionStart kept a waiter
that took the owner's answers, because no later waiter ever replaced it.

ONE WAITER PER CLAUDE PROCESS
Each start writes boards/<slug>/waiters/<claude pid>.json with its own pid; the
newest wins and an older one exits 0, silently, within a poll. Every other way
to end is silent too, because exit 0 with no output wakes nothing: the session's
`claude` is gone, the 21-day deadline, a project with no board, a board of another
project of the same folder name, a role that is never told. After 21 days idle the
waiter ends and the keeper is deaf until its next hook starts one: every other
session says so to the owner, once per answer, without the answer (answer_ledger.
unheard_note, printed by the prompt hook), and the ruling stays strict.

THE ONE WAY THIS CAN GO WRONG, AND WHAT RECOVERS IT
An answer is marked told (answer_ledger.claim) and the message never reaches the
agent. `/restart` re-execs Claude Code in the same process, so a waiter from
before it survives, with the same `claude` pid, and nothing reads what it prints
(spike S5: the exit 2 of such a waiter went to a defunct child and the restarted
session did not wake). Three guards, all asked again inside claim's lock:
  1. its own stdout and stderr, sockets to Claude Code, report a hang-up the
     moment the old image is gone (0.3 s before SessionStart(resume) fired, at
     the first restart and at the second); a waiter that sees it is deaf and
     claims nothing. Independent of any text, so it holds for any number of
     restarts;
  2. the `claude` command line changed (a restart rewrites it; weaker, because
     a later restart need not change it again);
  3. if the write of the message fails anyway, claim puts the record back.
The SessionStart(resume) waiter then claims what landed meanwhile.
What the guards do not close is a window: from the last hang-up check inside
claim until the old image closes its end of the socket. If the old image has
stopped reading hook output but has not closed the socket yet, the write
succeeds into the socket buffer, nothing raises, the marker stays written and
the exit 2 goes to nobody. Its length is the old image's teardown before the
exec, unmeasured; a tap in the same fraction of a second as the restart would
fall into it (three restarts with answers 0.09 to 0.58 s after Enter did not).
The same holds if a session is closed between the marker's write and Claude
Code reading the block. NOTHING recovers such an answer except the skill's rule
that the agent reads answers.json before it asks the owner anything, and
compares it with `answered` in narrative.json. A log of what was told, checked
against the session's transcript, was designed (KEEPER-DESIGN.md F4/F5) and not
built, to keep that change small.

Prints nothing on stdout, ever: an async hook's stdout is delivered with the next
turn, which would be a second delivery. Every error exits 0 and says nothing; the
prompt hook notices a waiter that is not running and tells the agent once.

Env, for tests only: ANSWER_WAITER_ANCHOR_PID, ANSWER_WAITER_POLL_S,
ANSWER_WAITER_WAIT_S, ANSWER_WAITER_RECHECK_S, ANSWER_WAITER_ORCA_ROLE.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True  # never write into the plugin directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "status-board"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_POLL_S = 1.0
DEFAULT_RECHECK_S = 5.0  # how often a waiter that is not the keeper asks again: the keeper's closing changes no file
# 21 days, an hour under hooks.json's timeout (1 814 400 s): Claude Code enforces `timeout` on an asyncRewake hook
# (a control hook with timeout 20 was killed at 20 s, spike of 14 Nov 2026), and a waiter killed from outside would
# end mid-claim. A session can stay idle for days and its keeper must still be woken by the owner's tap (the review,
# finding 4). Not longer: a timer above 2^31-1 ms (24.8 days) is clamped by Claude Code 2.1.325, which says so in its
# debug log ("timer delay exceeds the setTimeout range; clamped"), so 21 days is the largest round figure under it.
# What a waiter costs while it waits: about 0.01 s of CPU per 30 s and 22 MB, one per open session of a project that
# has a board (measured 14 Nov 2026, the default 1 s poll).
DEFAULT_WAIT_S = 1_810_800  # 21 days minus one hour


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def orca_worker(payload: dict) -> bool:
    """True when this session is a worker that Orca dispatched (see
    orca_session.py). Imported from beside this file; any failure means no."""
    try:
        import orca_session

        return orca_session.is_worker(payload)
    except Exception:
        return False


def role_of(payload: dict, command: str | None) -> str:
    """What kind of session this is: 'headless' (a `claude -p` run), 'teammate'
    (a split-pane teammate: a process launched with --agent-id and
    --parent-session-id), 'worker' (dispatched by Orca) or 'session'. Only a
    session is ever told. `agent_type` alone is no sign: a main session started
    with --agent has it."""
    import answer_ledger as ledger

    if ledger.is_headless(command):
        return "headless"
    try:
        import orca_session

        if orca_session.teammate_parent() is not None:
            return "teammate"
    except Exception:
        pass
    return "worker" if orca_worker(payload) else "session"


def orca_role_of(ledger, board: Path, anchor: int, payload: dict) -> str | None:
    """'coordinator' when an Orca Run names this session's terminal as its
    coordinator (the keeper's third fallback), else None. A positive answer is
    kept from this session's last registration; a negative one is asked again,
    because Orca may not have listed the Run yet at SessionStart."""
    forced = os.environ.get("ANSWER_WAITER_ORCA_ROLE")
    if forced:
        return forced
    previous = ledger.read_registration(board, anchor)
    if previous and previous.get("session_id") == payload.get("session_id") and previous.get("orca_role") == "coordinator":
        return "coordinator"
    try:
        import orca_session

        return "coordinator" if orca_session.is_coordinator() else None
    except Exception:
        return None


def hung_up() -> bool:
    """True when nobody can read what this process prints. Our stdout and
    stderr are sockets to the Claude Code image that started us; when that
    image is replaced (/restart re-execs it in place) or gone, the peer closes
    and poll reports POLLHUP. A live waiter reports nothing (spike S5). Can't
    tell: assume deaf, which only costs the fast path, never an answer."""
    try:
        poller = select.poll()
        for fd in (1, 2):
            poller.register(fd, select.POLLOUT)
        bad = select.POLLERR | select.POLLHUP | select.POLLNVAL
        return any(events & bad for _, events in poller.poll(0))
    except (OSError, ValueError):
        return True


def deliver(block: str) -> None:
    """Write straight to fd 2, not through sys.stderr: a buffered writer would
    swallow the error that says the reader is gone, and flush it at exit."""
    data = block.encode("utf-8")
    while data:
        data = data[os.write(2, data):]


def stat_sig(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def main() -> int:
    raw = sys.stdin.read()  # first: the runner writes the payload and closes it
    try:
        loaded = json.loads(raw)
    except ValueError:
        loaded = {}
    payload = loaded if isinstance(loaded, dict) else {}
    cwd = Path(payload["cwd"]) if payload.get("cwd") else Path.cwd()

    import answer_ledger as ledger

    board = ledger.board_for(cwd)
    if board is None or not board.is_dir():
        return 0  # not a board project, or another project's board: pay nothing, write nothing
    if payload.get("agent_id") or payload.get("teammate_name"):
        return 0  # fires inside a subagent or an in-process teammate, in its lead's claude: touch nothing of the lead's
    anchor = ledger.find_anchor()
    if anchor is None:
        return 0  # the prompt hook will say that the fast path is missing
    command = ledger.anchor_command(anchor)
    me = os.getpid()
    session_id = payload.get("session_id")
    # Milliseconds: "the session most recently in use" is decided on this, and two sessions can start in one second.
    now_iso = datetime.now().astimezone().isoformat(timespec="milliseconds")
    role = role_of(payload, command)
    if role != "session":
        # Said in the registration, then gone: an older waiter of this claude (one that a miss at
        # SessionStart let register as a session) finds a pid that is not its own and stops (F2).
        ledger.register(board, anchor, {"pid": me, "session_id": session_id, "event": payload.get("hook_event_name"),
                                        "started": now_iso, "role": role, "orca_role": None})
        ledger.mark_ended(board, anchor, me, role)
        return 0
    started = ledger.session_start(payload)
    # No sweep at Stop: a change that no tool call of this session was seen making records nobody (the review of
    # 14 Nov 2026, finding 3). Who wrote the board is decided only by hooks/board-keeper.py, from the call itself.
    info = {
        "pid": me,
        "session_id": session_id,
        "event": payload.get("hook_event_name"),
        "started": now_iso,
        "role": "session",
        "orca_role": orca_role_of(ledger, board, anchor, payload),
        # Where the session writes its transcript: what the unheard note reads to tell a keeper that is in the middle
        # of a long turn from one that is idle (the re-check of 15 Nov 2026, finding 3).
        "transcript": payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str) else None,
    }
    if not ledger.register(board, anchor, info):
        return 0

    def mine(strict: bool = False) -> bool:
        reg = ledger.read_registration(board, anchor)
        if not (reg and reg.get("pid") == me and ledger.pid_alive(anchor)) or hung_up():
            return False
        # `ps` per poll would cost a process a second; once, inside the lock, is free.
        return not strict or ledger.anchor_command(anchor) == command

    def mine_and_told() -> bool:
        return mine(strict=True) and ledger.is_keeper(board, session_id, anchor)

    poll_s, end = env_float("ANSWER_WAITER_POLL_S", DEFAULT_POLL_S), time.time() + env_float("ANSWER_WAITER_WAIT_S", DEFAULT_WAIT_S)
    recheck_s = env_float("ANSWER_WAITER_RECHECK_S", DEFAULT_RECHECK_S)
    watched = (board / "answers.json", board / ledger.MARKER, board / ledger.KEEPER)

    def wait_loop() -> tuple[int, str]:
        last, last_try = None, 0.0
        while True:
            if time.time() > end:
                return 0, "deadline"
            if not mine():
                return 0, "replaced, or the session is gone or restarted"
            # JSON is parsed only when one of the files changed, or every few seconds while an answer is waiting
            # (the keeper closing changes no file). The first round always claims, which catches whatever landed
            # while nothing was waiting.
            sig = tuple(stat_sig(path) for path in watched)
            if sig != last or (time.time() - last_try >= recheck_s and ledger.peek_fresh(board)):
                last, last_try = sig, time.time()
                if ledger.claim(board, started, mine_and_told, deliver=deliver):
                    return 2, "woke the session"  # claim already wrote the block to stderr
                if not mine(strict=True):
                    return 0, "deaf: its session was restarted"  # the next waiter takes over
            time.sleep(poll_s)

    code, reason = wait_loop()
    # Said before exiting, so that the prompt hook (which the wake itself fires)
    # does not take a waiter that has just done its job for a missing one.
    ledger.mark_ended(board, anchor, me, reason)
    return code


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - a reporting hook must never fail the session it reports on
        code = 0
    sys.exit(code)
