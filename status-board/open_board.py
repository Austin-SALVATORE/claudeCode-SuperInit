#!/usr/bin/env python3
"""Open the project's board in the owner's browser, once, without the key being in an address, a link, drawn text or a command line.

    python3 open_board.py --start <the folder the session was started in> [--cleanup-after SECONDS]

Release 2.4.0 (a board answers only its own page). A board keeps its data and every write behind
a key, `servers/board.key` in the status-board home (32 random bytes, 0600, in a 0700 folder: only the owner's account can read it). The
browser never gets the key. It gets a TOKEN FOR THAT ONE BOARD AND THAT ONE RUN OF IT, which the board's own page keeps in the tab's own storage and
sends as a header to its own address (there is no cookie: a cookie would go to every port of 127.0.0.1, so to any server the owner visits there):

  1. It finds the project's board and its port the way `board_link.py` does (the same rules; it connects to nothing), and says so in one
     sentence when the board is not running.
  2. It reads the key with `front_door.read_key`, which refuses a key that is not the owner's, is readable by anyone else, or is too short.
  2b. It asks the port to PROVE it is this project's board, before anything is written or posted (version 2 of the proof): one
     `GET /_proof?c=<c>` to `127.0.0.1:<port>`, `c` being 32 random hex characters made for this run, with `Host: 127.0.0.1:<port>`, a
     deadline of 2 seconds in all, no redirect followed, at most 1 kB read. The board answers 200 `application/json` with
     `{"run": "<run>", "proof": "<64 lowercase hex>"}`: `run` is 16 lowercase hex characters that the server made when it started (not a
     secret; it changes at each start) and `proof` the HMAC-SHA256 over the ASCII string `proof:v2:<port>:<run>:<c>` with the same RAW key.
     The opener checks the shape of `run`, computes the digest with the `run` it was given and compares with `hmac.compare_digest` (a
     constant-time comparison). On anything but a match (a refusal, no answer, a timeout, another shape, a missing or malformed run, a wrong
     digest) it writes no form, opens nothing, says that the port is not answering as this project's board and exits 1. This closes the
     gap of a record whose pid is alive but is not the board (a reused pid): another program that holds the port cannot prove it, so it
     never receives the token. The request carries no secret: it is a GET with a random challenge, so the program that gets it learns
     nothing it can use (the digest it would have to make needs the key).
  3. It makes the token, `v2.<port>.<run>.<issued>.<digest>` (version 2 of the token): `issued` is `int(time.time())`, `port` the
     board's, both plain decimal, `run` the one the proof gave, and `digest` the 64 lowercase hex characters of HMAC-SHA256 whose key is
     the RAW 32 bytes of the key file and whose message is the ASCII string `browser:v2:<port>:<run>:<issued>`. The token is bound to the
     port AND to the board's run: it is worthless on another board, and it dies with the server that issued it, so a program that takes a
     stopped board's port and keeps a token it was sent (or reads one from a tab's storage) holds nothing the board will take when it is back.
  4. It writes `servers/open-<16 random hex>.html`: created O_EXCL with mode 0600 in the 0700 folder, holding one form that posts the token
     to `http://127.0.0.1:<port>/_open` (exactly two hidden fields: `t`, the token, and `n`, the random part of the file's name), submitted by
     one line of script, with a button for a browser that runs none. The key is not in the form.
  5. It runs the system's opener (`open` on macOS, `xdg-open` elsewhere) on the FILE. The command line holds the file's path and nothing else.
     A FRESH TAB, never one the board's origin already holds (a board is opened from Claude Code for each browser tab): the file's name is
     new at each run (16 random hex characters), so no tab can
     already be on that address, and `open` / `xdg-open` hand a file to the default browser as a new document, which the browser opens in a
     tab or a window of its own; none navigates a tab that is already there (reasoned from the unique address; no real browser was run). The
     form posts in the tab the file opened in (it has no `target`), carries no script but the one line that submits it, and uses no storage,
     `window.open`, BroadcastChannel or `postMessage`: nothing in it can put the token into another tab. What the board's answer page
     then stores, and where, is the board's (a tab's own `sessionStorage`).
  6. It removes the file after `--cleanup-after` seconds (60), from a detached child, the cleaner, whose command line holds its code and the
     number and NOT the path: the path is written to the cleaner's standard input, not as an argument (the path holds the nonce `n`, and `ps`
     shows every command line to every account). The board removes the file earlier, when the form has been
     used (it takes the file's age from its modification time: at most 60 seconds).
  What cannot be avoided: the system's opener needs the path, so the path is on ITS command line, which is on every account's `ps` for as long as
  the opener runs (a moment: `open` returns at once). So the nonce `n` is not a secret, and nothing here relies on it being one. The server's half
  (the board's own server) is what makes knowing `n` useless: the token is checked before the file is taken (`POST /_open` needs a token that only the key can make,
  so a made-up or an old one fails first), so a post with a known `n` and a worthless token neither opens anything nor uses up the owner's opening.

It prints exactly one sentence, never an address (no scheme, host, port or key: what it says is toasted by the row and repeated by a session) and
never the key, and writes nothing to stderr. The key is nowhere in what it makes: not in the form, not in an
address, not on a command line, not in text that a terminal draws, not in a helper's output; the token it does write is good for one board
only, for 30 days from `issued` at most and only while that run of the board lasts (and `POST /_open` takes it only within 120 seconds of `issued`,
so a form left behind is dead in minutes).
What the opener sends to the network is that one GET (a random challenge, no key, no token), to the board's own port and to nothing else: it
never connects to the front door. The form's post is the browser's, made only after the proof.

Exit code: 0 when the browser was asked to open the form; 1 otherwise (the sentence says why).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEY_BYTES = 32
OPENED = "Opened the board in your browser."
NOT_RUNNING = "The board is not running; it starts with the next session in this project."
NO_OPENER = "No browser opener (open or xdg-open) is available here, so the board cannot be opened from Claude Code on this machine."
# What a person does comes first and the diagnosis last: the row toasts these sentences and Claude Code cuts a toast after about three lines of text
# (measured with 2.1.325), so a cut loses the diagnosis and never what to do.
#
# Each sentence promises only what the hook that starts a board at the start of a session does (hooks/revive-board.py): it asks the board for its proof
# and restarts the board when the server on the port is this project's own and cannot give it, and says so when something else holds the port.
#
# A key file that is there and is not private or not usable: the hook stops nothing and says what to do, because a restart cannot help (a new server
# refuses that key too); once the key is closed or deleted, a new session restarts the board if it still cannot prove itself.
NO_KEY = ("Make servers/board.key in the status-board home readable by its owner only, or delete it, then start a new session in this project, "
          "which restarts the board if it cannot prove itself: this board's key file is not private or not usable.")
# No key file at all: a board of an earlier release is running (it never made one), or the file was deleted while the board runs. There is nothing
# to make private and nothing to delete: a new session restarts the board, and the new server makes the key.
NO_KEY_FILE = ("Start a new session in this project: it restarts this board, which makes the key. This board has no key because it was started "
               "by an earlier release or its key file was deleted.")
NO_FORM = ("The page that opens the board could not be written in the servers folder of the status-board home, so nothing was opened; "
           "that folder must be yours and writable by you.")
# The port does not prove it holds the key: an older board, a board that holds another key, or another program is on it. A new session restarts the
# board when the server on the port is this project's own recorded server, and says so when something else holds the port.
NOT_PROVEN = ("Start a new session in this project: it restarts this board if the server on this port is this project's own, and if something else "
              "holds the port, that session says so. This port is not answering as this project's board, so nothing was opened.")
# An exception of the opener's own: not a board that is down, not a key. Never a traceback and never the exception's text.
FAILED = ("Try the button once more, and tell the session if it happens again: the opener stopped on a problem of its own, so nothing "
          "was opened.")
PROOF_DEADLINE = 2.0  # seconds, for the whole exchange
PROOF_MAX_BYTES = 1024
# The cleaner: its code and the number of seconds are on its command line, which every account's `ps` shows; the form's path (it holds the nonce
# `n`) is NOT: the opener writes it to the cleaner's standard input. It removes that one file, and only a file named like a form of ours.
CLEANER = ("import os, sys, time\npath = sys.stdin.readline().strip()\ntime.sleep(float(sys.argv[1]))\n"
           "if os.path.basename(path).startswith('open-'):\n    try:\n        os.unlink(path)\n    except OSError:\n        pass\n")


def say(text: str) -> int:
    sys.stdout.write(text + "\n")
    return 0 if text == OPENED else 1


def proof_digest(key: bytes, port: int, run: str, challenge: str) -> str:
    """The digest a board holding the key gives for a challenge: lowercase hexdigest of HMAC-SHA256 over ASCII `proof:v2:<port>:<run>:<c>`, RAW key."""
    return hmac.new(key, f"proof:v2:{int(port)}:{run}:{challenge}".encode(), hashlib.sha256).hexdigest()


def board_proves_itself(key: bytes, port: int) -> str | None:
    """One GET /_proof?c=<32 hex> to the board's port; the board's run (16 lowercase hex) when the answer is 200 application/json holding a run and
    the digest that only the key makes for that run, that port and this challenge, otherwise None. Never an exception: 2 seconds in all (a
    watchdog shuts the socket), no redirect followed, 1 kB at most."""
    kind, run = proof_outcome(key, port)
    return run if kind == "proven" else None


def proof_outcome(key: bytes, port: int) -> tuple[str, str | None]:
    """The same one request as `board_proves_itself`, told apart by what came back, as (kind, run); `run` only for `proven`:

      proven  200, application/json, a 16-hex `run`, and the digest only the key makes for that run, that port and this challenge;
      old     404, or 200 with the page or with anything that is not that JSON: no proof route (a 2.3.1 server, or not a board);
      nokey   503: a server of this release that has no usable key;
      wrong   200 in the right shape with a digest that is not the key's: an older key, or not the board;
      silent  a timeout, a redirect, any other status, more than 1 kB, a refused or reset connection: not proven, and nothing more is known.

    The hook that starts a board at the start of a session uses the kind to decide whether the board is to be restarted. Never an exception: 2 seconds
    in all (a watchdog shuts the socket), no redirect followed, 1 kB at most, no secret sent."""
    challenge = secrets.token_hex(16)
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=PROOF_DEADLINE)
    held: list = []  # the socket, kept here: `conn.sock` is None once the answer is being read (Connection: close)

    def cut() -> None:
        for sock in held:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(PROOF_DEADLINE, cut)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.connect()
        held.append(conn.sock)
        conn.request("GET", f"/_proof?c={challenge}", headers={"Host": f"127.0.0.1:{int(port)}", "Connection": "close", "Accept": "application/json"})
        answer = conn.getresponse()
        if answer.status == 404:
            return "old", None
        if answer.status == 503:
            return "nokey", None
        if answer.status != 200:
            return "silent", None  # a redirect, a refusal of another kind: nothing is known
        if not answer.getheader("Content-Type", "").lower().startswith("application/json"):
            return "old", None  # the page (or anything that is not that JSON): there is no proof route
        body = answer.read(PROOF_MAX_BYTES + 1)
        if len(body) > PROOF_MAX_BYTES:
            return "silent", None
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            return "old", None
        run = data.get("run") if isinstance(data, dict) else None
        given = data.get("proof") if isinstance(data, dict) else None
        if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{16}", run):
            return "old", None
        if not isinstance(given, str) or not re.fullmatch(r"[0-9a-f]{64}", given):
            return "old", None
        expected = proof_digest(key, port, run, challenge)
        if hmac.compare_digest(given.encode("ascii"), expected.encode("ascii")):
            return "proven", run
        return "wrong", None
    except (OSError, http.client.HTTPException, ValueError, UnicodeError):
        return "silent", None
    finally:
        watchdog.cancel()
        conn.close()


def find_row(start: Path):
    """(the board, the row of its server) as `board_link.link_for` finds them, or None: no connection, nothing written."""
    sys.dont_write_bytecode = True
    os.environ["CLAUDE_PROJECT_DIR"] = str(start)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import answer_ledger as ledger
    import board_link
    import front_door

    board, folder, _refused = ledger.find_board(Path(start))
    if board is None or folder is None:
        return None
    front_door.is_up = board_link._pid_alive  # no connection, no lsof: the process is alive
    door = front_door.FrontDoor(types.SimpleNamespace(slug_for=ledger.slug_for, STATE=ledger.STATE, HERE=HERE),
                                state=ledger.STATE, verify=lambda rec: True)
    kind, rows = door.resolve(board.name)
    if kind != "one" or not rows or not rows[0].get("known") or not rows[0].get("port"):
        return None
    row = rows[0]
    recorded = ledger._recorded_repo(board)
    if Path(row["repo"]).resolve() != Path(recorded or folder).resolve():
        return None  # the door found another project's board by that name
    return board, row, ledger.STATE


def browser_token(key: bytes, port: int, run: str, issued: int) -> str:
    """`v2.<port>.<run>.<issued>.<digest>`, version 2 of the token: plain decimal numbers, `run` the board's (16 lowercase hex), the digest
    the lowercase `hexdigest()` of HMAC-SHA256 over the ASCII string `browser:v2:<port>:<run>:<issued>` with the RAW bytes of the key (never
    its hex text)."""
    digest = hmac.new(key, f"browser:v2:{int(port)}:{run}:{int(issued)}".encode(), hashlib.sha256).hexdigest()
    return f"v2.{int(port)}.{run}.{int(issued)}.{digest}"


def form_for(port: int, token: str, nonce: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
        "<title>Opening the board</title></head><body>"
        f'<form method="post" action="http://127.0.0.1:{int(port)}/_open" enctype="application/x-www-form-urlencoded">'
        f'<input type="hidden" name="t" value="{token}"><input type="hidden" name="n" value="{nonce}">'
        '<noscript><button type="submit">Open the board</button></noscript></form>'
        "<script>document.forms[0].submit()</script></body></html>\n"
    )


def write_form(servers: Path, text: str, nonce: str) -> Path | None:
    path = servers / f"open-{nonce}.html"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
    except OSError:
        remove(path)  # a half-written form is not left in the folder
        return None
    return path


def remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--start")
    parser.add_argument("--cleanup-after", type=float, default=60.0)
    try:
        args, _unknown = parser.parse_known_args()
        if not args.start or not Path(args.start).is_absolute() or not Path(args.start).is_dir():
            return say(NOT_RUNNING)
        found = find_row(Path(args.start).resolve())
        if found is None or not found[1].get("up"):
            return say(NOT_RUNNING)
        board, row, state = found
        import front_door

        servers = Path(state) / "servers"
        if not front_door.private_dir(servers):
            return say(NO_FORM)  # the folder is not the account's: the key's fix would be the wrong one
        if not os.path.lexists(servers / front_door.BOARD_KEY_FILE):
            return say(NO_KEY_FILE)  # nothing to make private or delete: see NO_KEY_FILE
        key = front_door.read_key(state, front_door.BOARD_KEY_FILE)
        if key is None or len(key) != KEY_BYTES:
            return say(NO_KEY)
        run = board_proves_itself(key, int(row["port"]))
        if run is None:
            return say(NOT_PROVEN)
        nonce = secrets.token_hex(8)
        token = browser_token(key, row["port"], run, int(time.time()))
        path = write_form(servers, form_for(row["port"], token, nonce), nonce)
        if path is None:
            return say(NO_FORM)
        opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
        if opener is None:
            remove(path)
            return say(NO_OPENER)
        try:
            done = subprocess.run([opener, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                                  check=False)
        except (OSError, subprocess.SubprocessError):
            remove(path)
            return say(NO_OPENER)
        if done.returncode != 0:
            remove(path)
            return say(NO_OPENER)
        try:
            cleaner = subprocess.Popen([sys.executable, "-c", CLEANER, str(max(0.0, args.cleanup_after))], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            if cleaner.stdin is not None:
                cleaner.stdin.write(os.fsencode(path) + b"\n")  # the path goes in by the standard input, never on a command line
                cleaner.stdin.close()
        except OSError:
            pass  # the board removes the form when it is used; a form left for the owner's account only is the cost
        return say(OPENED)
    except BaseException:  # noqa: BLE001 - never a traceback, never the key, never the text of the exception (it could hold either)
        return say(FAILED)  # a problem of the opener's own: not "the board is not running", which is said only where the board is found down


if __name__ == "__main__":
    sys.exit(main())
