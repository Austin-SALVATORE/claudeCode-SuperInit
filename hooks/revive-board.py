#!/usr/bin/env python3
"""Restart a project's status board when nothing is answering on its port.

A board's state survives a reboot (it lives in ~/.claude/status-board/boards/
<slug>/); the server that serves it does not. After a restart the owner found
out by opening a dead URL, and the fix was someone remembering the exact
command. This starts the board again at session start, so the URL in the
owner's browser works without anyone being asked.

What it does, in order:
  1. The project has a board — boards/<slug>/narrative.json exists — or stop.
  2. Probe the board's port (the one serve.py itself derives):
       the board page answers  -> do nothing;
       something else answers  -> start nothing, print one line saying so;
       nothing is listening    -> start serve.py, detached, and print the URL.

serve.py's own `slug_for`, `port_for` and `STATE` are imported, not copied, so
the hook cannot drift from the server: this is the third copy of slug_for in
hooks/ that would otherwise have to "match exactly", and the port is a crc32 of
the whole resolved path, which is worse to get subtly wrong. Importing it
defines functions and constants and touches nothing (checked: a tree
fingerprint of the plugin dir, the home and the state dir is unchanged by it).

Detached means a new session (setsid), stdin from /dev/null and stdout/stderr
in boards/<slug>/serve.log — never the hook's own pipes. A child that inherits
them keeps the session start waiting on a pipe that only closes when the
server dies. A short lock file (boards/<slug>/.revive.lock) makes two sessions
that start together launch one server rather than one server and one traceback.

"Answers" means `GET /` returns 200 with the board page, not `GET /report.json`
as SKILL.md suggests for a person: report() runs `git fetch` in the project, and
a session start should not trigger network I/O to find out if a page is up.

Limits, stated rather than hidden:
  * serve.py does not store the display name (`--name` is display-only), so a
    revived board uses its default, the folder name.
  * It restarts with serve.py's defaults: loopback only. A board that had been
    exposed with --host needs --host again.
  * The page is the same for every project, so a *different* project's board
    that has the same derived port (120 ports, collisions happen) looks like
    this one and is left alone.
  * It runs in every session type; it is idempotent, and a teammate starts
    after its lead, by which time the board is up.

Never blocks the session: connections time out in under a second, the wait for
a new server is capped at a few seconds, and every path exits 0. When it cannot
start the board it prints one line pointing at the log; it never fails silently
into "the board is probably up". Run by hand with:
    echo '{"cwd": "/path/to/project"}' | python3 hooks/revive-board.py
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVE_PY = PLUGIN_ROOT / "status-board" / "serve.py"
HOST = "127.0.0.1"
BOARD_PAGE = b"<title>status board</title>"  # index.html, line 6
START_WAIT_S = 3.0  # longest the session start waits for a server to come up
LOCK_STALE_S = 20.0  # a lock older than this belongs to a hook that died


def read_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        data = json.loads(sys.stdin.read(1_000_000))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_serve():
    """serve.py as a module, without writing bytecode into the plugin dir.

    It does a sibling `import tree_lock`, so its own directory goes on sys.path;
    the `__main__` guard keeps the server from starting."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SERVE_PY.parent))
    spec = importlib.util.spec_from_file_location("_project_init_serve", SERVE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_toplevel(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def find_board(serve, cwd: Path, home: Path):
    """(repo, slug, board_dir) for the first root that has a board, else None.

    The git top-level goes first: the board was started for the repo root, and
    a subfolder called `app` or `api` must not claim another project's board
    that happens to share its name."""
    roots = []
    top = git_toplevel(cwd)
    if top is not None and top not in (home, Path(top.anchor)):
        roots.append(top)
    roots.append(cwd)
    for root in dict.fromkeys(roots):
        slug = serve.slug_for(root)
        board_dir = Path(serve.STATE) / "boards" / slug
        if (board_dir / "narrative.json").is_file():
            return root, slug, board_dir
    return None


def probe(port: int) -> str:
    """'free' (nothing listening), 'board' (the board page answers) or 'other'
    (something is there and is not the board, including something that
    accepts the connection and never replies — bounded by the 1 s timeout)."""
    try:
        conn = http.client.HTTPConnection(HOST, port, timeout=1.0)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            head = resp.read(4096)
        finally:
            conn.close()
    except ConnectionRefusedError:
        return "free"
    except (OSError, http.client.HTTPException):
        return "other"
    return "board" if resp.status == 200 and BOARD_PAGE in head.lower() else "other"


def take_lock(lock: Path) -> bool:
    """True if we may start the server; False if another hook is doing it."""
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < LOCK_STALE_S:
                    return False
                lock.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return True  # cannot lock (unwritable dir): start anyway
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def start(serve, repo: Path, slug: str, port: int, board_dir: Path) -> str:
    """Launch serve.py detached; return the one line to print."""
    log_path = board_dir / "serve.log"
    try:
        log = open(log_path, "ab")
        log.write(
            f"--- {datetime.now().astimezone().isoformat(timespec='seconds')} "
            f"revive-board.py: serve.py --repo {repo} --port {port}\n".encode()
        )
        log.flush()
    except OSError:
        log = None  # unwritable board dir: start it anyway, just unlogged
    env = dict(os.environ, STATUS_BOARD_HOME=str(Path(serve.STATE).resolve()))
    try:
        proc = subprocess.Popen(
            [sys.executable or "python3", str(SERVE_PY), "--repo", str(repo), "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=log if log is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            cwd=str(board_dir),
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        return f'project-init: status board "{slug}" could not be started ({exc}); see {log_path}'
    finally:
        if log is not None:
            log.close()  # the child holds its own descriptor
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return (
                f'project-init: status board "{slug}" exited at start '
                f"(code {proc.returncode}); see {log_path}"
            )
        if probe(port) == "board":
            return (
                f'project-init: status board "{slug}" was not running; '
                f"started it again: http://{HOST}:{port}"
            )
        time.sleep(0.1)
    return (
        f'project-init: status board "{slug}" was launched but is not answering '
        f"on port {port} yet; see {log_path}"
    )


def main() -> int:
    payload = read_payload()
    raw = payload.get("cwd")
    if not isinstance(raw, str) or not raw:
        return 0
    cwd, home = Path(raw).resolve(), Path.home().resolve()
    if not cwd.is_dir():
        return 0
    try:
        serve = load_serve()
    except Exception:
        return 0
    found = find_board(serve, cwd, home)
    if found is None:
        return 0
    repo, slug, board_dir = found
    port = serve.port_for(repo)

    state = probe(port)
    if state == "board":
        return 0
    if state == "other":
        print(
            f'project-init: status board "{slug}" not started: port {port} is held '
            "by something that is not this board. Free the port, or start the "
            "board by hand with --port."
        )
        return 0

    lock = board_dir / ".revive.lock"
    if not take_lock(lock):
        return 0
    try:
        if probe(port) == "free":  # another hook may have won while we waited
            print(start(serve, repo, slug, port, board_dir))
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
