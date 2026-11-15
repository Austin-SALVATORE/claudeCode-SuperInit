#!/usr/bin/env python3
"""The board's short address for a session, or nothing (release 2.2.0, part C, 15 Nov 2026).

    python3 board_link.py --start <the folder the session was started in> [--no-agent-check]   (a payload on stdin, optional)

The mod `hooks/board-link.tsx` draws a row above the prompt with one button, labelled with the address `localhost:8800/<name>` of the
project's own board, that opens the board. A mod runs in a sandbox without Python, and the rules for which project, which board and
which name are Python and already written, so the mod asks this helper and draws what it prints. It prints one line of
JSON, or nothing (any error, and a Claude Code of any version that did not ask):

    {"state": "<the status-board home it read>", "url": "<the address>" | null, "up": true | false | null, "agent": true?,
     "port": <the board's port> | null, "data": "<the plugin's data folder>", "seen_paths": ["<a file of its seen/ folder>", ...],
     "status_line_seen": true | false}

`state`, `url`, `up` and `agent`, and the flags `--start` and `--no-agent-check`, are a contract with status line filters, the
shipped one and hand-made ones: they and the command line are kept as they are (a hand-made filter reads `url` and `up` and links
to `url`: the short address, as before). `port` is release 2.4.0's, a new key beside them: the port of the board's own server, from its
record, so that the shipped status line can link to `http://127.0.0.1:<port>/`, the board itself, and never to `localhost` (a browser given
`localhost` may try `::1` first, where another program can listen unless the front door, which holds `::1` as well as 127.0.0.1 while it
runs, got there first). It is null wherever `url` is. The `data`, `seen_paths` and
`status_line_seen` keys are release 2.3.0's (where the link shows is a choice, made at set-up): the
status line's filter touches `seen/<first 16 hex of sha1 of the start folder's path>` in the plugin's data folder each time it
places the link, and `status_line_seen` says whether that file is younger than ten minutes. The button above the prompt no longer
reads them (it is drawn whenever either board link switch is on); they are kept as a contract with hand-made readers.
`seen_paths` are the files that say it.

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

import hashlib
import json
import os
import re
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_KEYS = ("agent_type", "agent_id", "teammate_name", "team_name")  # as hooks/offer-bootstrap.py reads them
SEEN_SECONDS = 600  # the link counts as shown in the status line for ten minutes after the filter last placed it


def data_dir() -> Path:
    """The plugin's data folder, where the status line's filter leaves `seen/<first 16 hex of sha1 of the start folder>`.

    The one place its path is worked out here. A command hook is given it as CLAUDE_PLUGIN_DATA; a mod, and the process a mod
    starts (this one), is not (spike of 15 Nov 2026, Claude Code 2.1.325, option/SPIKE.md section 5), so it is worked out from
    where this file is: installed from a marketplace, `<plugins>/cache/<marketplace>/<name>/<version>` gives
    `<plugins>/data/<name>-<marketplace>`; loaded by --plugin-dir, `<config>/plugins/data/<name>-inline`, the config folder
    being CLAUDE_CONFIG_DIR or ~/.claude. Characters other than letters, digits, `_` and `-` become `-`. Only a path: nothing
    here makes the folder.
    """
    given = os.environ.get("CLAUDE_PLUGIN_DATA")
    if given:
        return Path(given)
    root = HERE.parent
    market = root.parent.parent
    if market.parent.name == "cache":
        ident = f"{root.parent.name}-{market.name}"
        return market.parent.parent / "data" / re.sub(r"[^A-Za-z0-9_-]", "-", ident)
    name = root.name
    try:
        name = str(json.loads((root / ".claude-plugin" / "plugin.json").read_text()).get("name") or name)
    except (OSError, ValueError):
        pass
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return config / "plugins" / "data" / re.sub(r"[^A-Za-z0-9_-]", "-", f"{name}-inline")


def status_line_marker(start: Path, given: str | None) -> dict:
    """`data`: the data folder; `seen_paths`: the files whose time says when the filter last placed the link for this start
    folder (its path as given and as resolved: the filter may hash either); `status_line_seen`: one of them is younger than
    ten minutes. A folder or a file that is not there says: not seen."""
    folder = data_dir()
    forms = {str(start)}
    if given:
        forms.add(str(Path(given)))
    paths = [str(folder / "seen" / hashlib.sha1(form.encode()).hexdigest()[:16]) for form in sorted(forms)]
    seen = False
    for path in paths:
        try:
            if time.time() - os.stat(path).st_mtime < SEEN_SECONDS:
                seen = True
        except OSError:
            pass
    return {"data": str(folder), "seen_paths": paths, "status_line_seen": seen}


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


def link_for(start: Path, payload: dict | None = None, check_agent: bool = True, given: str | None = None) -> dict:
    sys.dont_write_bytecode = True
    os.environ["CLAUDE_PROJECT_DIR"] = str(start)  # the folder the session was started in: answer_ledger.start_folder
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import answer_ledger as ledger
    import front_door

    out: dict = {"state": str(ledger.STATE), "url": None, "up": None, "port": None}
    out.update(status_line_marker(start, given))  # release 2.3.0: the keys above are the status line's contract, these are new
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
    port = row.get("port")
    out["port"] = port if type(port) is int and 0 < port < 65536 else None  # the board's own server, for a link that does not go through the door
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
        sys.stdout.write(json.dumps(link_for(start.resolve(), payload, check_agent=not no_agent_check, given=start_arg)) + "\n")
    except BaseException:  # noqa: BLE001 - never fail the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
