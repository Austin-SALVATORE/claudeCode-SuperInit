#!/usr/bin/env python3
"""The board's short address for a session, or nothing (release 2.2.0, part C, 15 Nov 2026).

    python3 board_link.py --start <the folder the session was started in> [--no-agent-check]   (a payload on stdin, optional)

The mod `hooks/board-link.tsx` draws a row above the prompt with the address `http://localhost:8800/<name>` of the
project's own board, as a link. A mod runs in a sandbox without Python, and the rules for which project, which board and
which name are Python and already written, so the mod asks this helper and draws what it prints. It prints one line of
JSON, or nothing (any error, and a Claude Code of any version that did not ask):

    {"state": "<the status-board home it read>", "url": "<the address>" | null, "up": true | false | null, "agent": true?}

`url` is null where there is nothing to show: the folder is no project, the project has no board, the board has never been
started (no record: the front door lists it as "no address yet"), the board's name is not one the front door would send to
this project's board (another project holds it, or two do), or the home's boards are not behind the front door at all.
`up` is whether the server's process is alive: the mod marks the link "(not running)" when it is not. `agent` is true in a
worker, a teammate or a subagent, which see nothing. `state` is where the mod can watch for a change.

**Found the way 2.1.0 finds them, not a new way.** The board is `answer_ledger.find_board`, with the folder the session was
started in as CLAUDE_PROJECT_DIR (the start folder first, then the walk, repo.json: KEEPER-BUILD.md sections 13 to 15). The
name is the front door's own `FrontDoor.resolve`, called here, with the one difference that matters: it is told that a
server is up when its process is alive, and nothing connects. The door's own `is_up` opens a connection to the board's port
and asks lsof who listens; a link must not touch a board, and must not start a process of lsof in every session, so that one
function is replaced, in this process only, by a check of the pid. A worker is told from the owner's session as the other
hooks tell them (orca_session.py).

It reads: repo.json of the board's folder and the names of the folders under boards/ (answer_ledger, front_door), the
records under servers/ (front_door), and, to tell a worker, what orca_session reads. It never opens answers.json or
narrative.json, never writes a file, never connects to a board or to the front door. Fails quiet: any error prints nothing
and exits 0.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_KEYS = ("agent_type", "agent_id", "teammate_name", "team_name")  # as hooks/offer-bootstrap.py reads them


def _pid_alive(rec: dict) -> bool:
    """front_door.is_up without the connection: the record's process is alive."""
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # alive, someone else's
    return True


def agent_session(payload: dict) -> bool:
    """A worker Orca dispatched, a split-pane teammate, or anything whose payload names an agent."""
    if any(payload.get(key) for key in AGENT_KEYS):
        return True
    try:
        hooks = str(HERE.parent / "hooks")
        if hooks not in sys.path:
            sys.path.insert(0, hooks)
        import orca_session

        return orca_session.teammate_parent() is not None or orca_session.is_worker(payload)
    except Exception:  # noqa: BLE001 - any doubt is "not an agent"; the worse mistake is hiding the link from the owner
        return False


def link_for(start: Path, payload: dict | None = None, check_agent: bool = True) -> dict:
    sys.dont_write_bytecode = True
    os.environ["CLAUDE_PROJECT_DIR"] = str(start)  # the folder the session was started in: answer_ledger.start_folder
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import answer_ledger as ledger
    import front_door

    out: dict = {"state": str(ledger.STATE), "url": None, "up": None}
    if check_agent and agent_session(payload or {}):
        out["agent"] = True
        return out
    board, folder, _refused = ledger.find_board(Path(start))
    if board is None or folder is None or not front_door.election_enabled(ledger.STATE):
        return out
    front_door.is_up = _pid_alive  # no connection, no lsof: see the docstring
    door = front_door.FrontDoor(types.SimpleNamespace(slug_for=ledger.slug_for, STATE=ledger.STATE, HERE=HERE),
                                state=ledger.STATE, verify=lambda rec: True)
    kind, rows = door.resolve(board.name)
    if kind != "one" or not rows or not rows[0].get("known") or not rows[0].get("port"):
        return out
    row = rows[0]
    # The front door found one board by that name; it must be this project's, or the link leads to another project's.
    recorded = ledger._recorded_repo(board)
    if Path(row["repo"]).resolve() != Path(recorded or folder).resolve():
        return out
    out["url"] = front_door.name_address(board.name)
    out["up"] = bool(row.get("up"))
    return out


def main() -> int:
    argv = sys.argv[1:]
    start_arg = argv[argv.index("--start") + 1] if "--start" in argv and argv.index("--start") + 1 < len(argv) else None
    no_agent_check = "--no-agent-check" in argv
    if start_arg is None:
        return 0
    try:
        raw = sys.stdin.read(1_000_000) if sys.stdin is not None and not sys.stdin.isatty() else ""
        try:
            loaded = json.loads(raw) if raw.strip() else {}
        except ValueError:
            loaded = {}
        payload = loaded if isinstance(loaded, dict) else {}
        start = Path(start_arg)
        if not start.is_absolute() or not start.is_dir():
            return 0
        sys.stdout.write(json.dumps(link_for(start.resolve(), payload, check_agent=not no_agent_check)) + "\n")
    except BaseException:  # noqa: BLE001 - never fail the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
