#!/usr/bin/env python3
"""A board must not be shown inside the page of another site (14 Nov 2026).

    python3 test_frame_headers.py --plugin <checkout of the plugin>
    python3 test_frame_headers.py --plugin <checkout> --show     # print every response's headers

The finding: neither the page nor any other response of serve.py sent
`X-Frame-Options` or a `frame-ancestors` policy, so any page the owner visits
could hold a board in an invisible iframe and take one click on its pill as
the owner's ruling (the POST then comes from the board's own page, so every
check on writes passes). These tests start real serve.py processes, on free
ports and a temporary STATUS_BOARD_HOME, and read the headers of every kind
of response the server sends.

Run it against the code before the fix and every test but the last fails;
that is how each was shown to be able to fail. Nothing here touches a live
board, a real repository or the real Orca: the served folder is a copy of the
plugin's status-board/ made inside the temporary folder.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import http.client
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from email.utils import formatdate
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="checkout of the plugin to test")
ap.add_argument("--show", action="store_true", help="print the headers of every response and exit")
ARGS = ap.parse_args()
PLUGIN = Path(ARGS.plugin).resolve()
BOARD_DIR = PLUGIN / "status-board"
sys.dont_write_bytecode = True

TMP = Path(tempfile.mkdtemp(prefix="board-frame-")).resolve()
REPO = TMP / "demo"
HOME_DIR = TMP / "home"
TREE = TMP / "tree" / "status-board"  # what is served: a copy, so the plugin under test is never written to
SAFE_PATH = f"/usr/bin:/bin:{Path(sys.executable).parent}"

NARRATIVE = {"writtenAt": "2026-11-14T00:00:00+01:00", "headline": "SECRET-HEADLINE",
             "decisions": [{"q": "Ship it?", "options": ["yes"]}]}
PICTURE_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20"/>'
               '<script>1</script></svg>')
JSON_HEADERS = {"Content-Type": "application/json"}
# The policy a shot is sent with today (serve.py: SHOT_POLICY). Written out here
# on purpose: the test must notice if the fix changes any part of it.
SHOT_POLICY_BEFORE = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
FRAME_POLICY = "frame-ancestors 'none'"


class Skip(Exception):
    """A test that cannot run on this machine, said out loud rather than passed."""


def setup() -> None:
    REPO.mkdir(parents=True)
    git = ["git", "-C", str(REPO), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "scratch"], check=True)
    board = HOME_DIR / "boards" / "demo"
    (board / "shots").mkdir(parents=True)
    (board / "narrative.json").write_text(json.dumps(NARRATIVE))
    (board / "shots" / "planted.svg").write_text(PICTURE_SVG)
    (board / "shots" / "dot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    shutil.copytree(BOARD_DIR, TREE, ignore=shutil.ignore_patterns("__pycache__"))
    (TREE / "sub").mkdir()  # a folder, to get a redirect and a listing out of the static handler
    (TREE / "sub" / "note.txt").write_text("a plain file\n")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lan_address() -> str | None:
    """The address other machines would use, as serve.py finds it; None when there is none."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 1))  # TEST-NET-1: nothing is sent
            address = s.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


class Server:
    """serve.py from the copy of the plugin under test, on a free port, on the scratch board."""

    def __init__(self, host: str = "127.0.0.1"):
        self.port = free_port()
        (HOME_DIR / "boards" / "demo" / "answers.json").unlink(missing_ok=True)
        env = {"HOME": str(TMP), "PATH": SAFE_PATH, "STATUS_BOARD_HOME": str(HOME_DIR),
               "STATUS_BOARD_ORCA": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        cmd = [sys.executable, str(TREE / "serve.py"), "--repo", str(REPO), "--port", str(self.port)]
        if host != "127.0.0.1":
            cmd += ["--host", host]
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(TMP))
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.3).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def raw(self, method: str, path: str, headers: dict | None = None, body: bytes | None = None,
            connect_to: str = "127.0.0.1") -> tuple[int, list[tuple[str, str]], bytes]:
        """One request with exactly these headers: (status, [(name, value), ...] with repeats kept, body)."""
        headers = headers or {}
        conn = http.client.HTTPConnection(connect_to, self.port, timeout=10)
        conn.putrequest(method, path, skip_host="Host" in headers)
        for name, value in headers.items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        res = conn.getresponse()
        data = res.read()
        pairs = [(name.lower(), value) for name, value in res.getheaders()]
        conn.close()
        return res.status, pairs, data

    def answers(self) -> dict:
        path = HOME_DIR / "boards" / "demo" / "answers.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)


def values(pairs: list[tuple[str, str]], name: str) -> list[str]:
    return [value for key, value in pairs if key == name.lower()]


def kinds(server: Server) -> list[tuple[str, int, str, str, dict, bytes | None]]:
    """Every kind of response the server sends: (label, status expected, method, path, headers, body)."""
    own = f"http://127.0.0.1:{server.port}"
    answer = json.dumps({"q": "Ship it?", "answer": "yes"}).encode()
    _, page_headers, _ = server.raw("GET", "/")
    last_modified = values(page_headers, "Last-Modified")[0]
    foreign = {"Host": f"evil.example:{server.port}"}
    return [
        ("the page", 200, "GET", "/", {}, None),
        ("the page, as /index.html", 200, "GET", "/index.html", {}, None),
        ("the page, head only", 200, "HEAD", "/", {}, None),
        ("the page, not modified", 304, "GET", "/", {"If-Modified-Since": last_modified}, None),
        ("report.json", 200, "GET", "/report.json", {}, None),
        ("report.json with a query", 200, "GET", "/report.json?t=1", {}, None),
        # HEAD is not routed to the report today: the static handler looks for a
        # file of that name and answers 404. Pinned as it is; not part of this change.
        ("report.json, head only (not routed: 404)", 404, "HEAD", "/report.json", {}, None),
        ("a picture under shots/", 200, "GET", "/shot/dot.png", {}, None),
        ("an svg under shots/", 200, "GET", "/shot/planted.svg", {}, None),
        ("a picture that is not there", 404, "GET", "/shot/gone.png", {}, None),
        ("a path that is not there", 404, "GET", "/nothing-here", {}, None),
        ("a plain file of the folder", 200, "GET", "/sub/note.txt", {}, None),
        ("a folder without its slash: a redirect", 301, "GET", "/sub", {}, None),
        ("a folder's listing", 200, "GET", "/sub/", {}, None),
        ("a read under another name: refused", 403, "GET", "/report.json", foreign, None),
        ("a head under another name: refused", 403, "HEAD", "/", foreign, None),
        ("an answer from the page: recorded", 201, "POST", "/answer", {**JSON_HEADERS, "Origin": own}, answer),
        ("an answer from another origin: refused", 403, "POST", "/answer", {**JSON_HEADERS, "Origin": "https://example.invalid"}, answer),
        ("an answer sent as text: refused", 415, "POST", "/answer", {"Content-Type": "text/plain"}, answer),
        ("an answer that is not JSON: refused", 400, "POST", "/answer", JSON_HEADERS, b"not json"),
        ("a post to a path that does not take one", 404, "POST", "/nope", JSON_HEADERS, b"{}"),
        ("OPTIONS: not supported", 501, "OPTIONS", "/", {}, None),
        ("PUT: not supported", 501, "PUT", "/", {}, b"x"),
        ("DELETE: not supported", 501, "DELETE", "/report.json", {}, None),
    ]


def show() -> None:
    setup()
    server = Server()
    try:
        for label, status, method, path, headers, body in kinds(server):
            code, pairs, _ = server.raw(method, path, headers, body)
            print(f"{method} {path}  [{label}]  -> {code}")
            for name, value in pairs:
                if name not in ("date", "server"):
                    print(f"    {name}: {value}")
    finally:
        server.stop()
        shutil.rmtree(TMP, ignore_errors=True)


# ── the tests ─────────────────────────────────────────────────────────
def test_F1_every_kind_of_response_forbids_being_framed():
    """Each response carries X-Frame-Options: DENY and a policy with frame-ancestors 'none', once each."""
    server = Server()
    faults: list[str] = []
    total = 0
    try:
        for label, status, method, path, headers, body in kinds(server):
            total += 1
            code, pairs, _ = server.raw(method, path, headers, body)
            assert code == status, f"{label}: expected {status}, got {code}"
            frame_options = values(pairs, "X-Frame-Options")
            policies = values(pairs, "Content-Security-Policy")
            if frame_options != ["DENY"]:
                faults.append(f"{label}: X-Frame-Options {frame_options}")
            directives = [d.strip() for p in policies for d in p.split(";")]
            if len(policies) != 1 or FRAME_POLICY not in directives:
                faults.append(f"{label}: Content-Security-Policy {policies}")
        assert server.answers()["Ship it?"]["answer"] == "yes", "the one valid answer must still be recorded"
        assert sorted(server.answers()["Ship it?"]) == ["answer", "at"], "answers.json changed shape"
    finally:
        server.stop()
    unprotected = len({fault.split(": ")[0] for fault in faults})
    assert not faults, f"{unprotected} of {total} kinds of response unprotected, e.g. " + "; ".join(faults[:3])


def test_F2_a_response_without_a_policy_gets_only_the_frame_rule():
    """The page and the rest get no policy for scripts or styles: that is another change."""
    server = Server()
    try:
        for path in ("/", "/report.json", "/nothing-here"):
            _, pairs, _ = server.raw("GET", path)
            assert values(pairs, "Content-Security-Policy") == [FRAME_POLICY], (path, values(pairs, "Content-Security-Policy"))
    finally:
        server.stop()


def test_F3_a_shot_keeps_its_sandbox_in_one_policy():
    """A picture under shots/ keeps every part of its policy and gains frame-ancestors, in the same header."""
    server = Server()
    try:
        for name, kind in (("planted.svg", "image/svg+xml"), ("dot.png", "image/png")):
            status, pairs, _ = server.raw("GET", f"/shot/{name}")
            policies = values(pairs, "Content-Security-Policy")
            assert status == 200 and values(pairs, "Content-Type") == [kind], (status, values(pairs, "Content-Type"))
            assert len(policies) == 1, f"{name}: {len(policies)} Content-Security-Policy headers, a browser would combine them: {policies}"
            policy = policies[0]
            assert policy.startswith(SHOT_POLICY_BEFORE), f"{name}: the shot's own policy was changed: {policy!r}"
            assert policy[len(SHOT_POLICY_BEFORE):].strip(" ;") == FRAME_POLICY, f"{name}: policy is {policy!r}"
            assert "allow-scripts" not in policy and "allow-same-origin" not in policy, policy
            assert values(pairs, "X-Content-Type-Options") == ["nosniff"], name
            assert values(pairs, "X-Frame-Options") == ["DENY"], name
    finally:
        server.stop()


def test_F4_the_refusal_of_a_machine_on_the_network_is_protected_too():
    """deny(): the 403 sent to an unauthorised client of a board bound beyond loopback, and a served page."""
    address = lan_address()
    if address is None:
        raise Skip("this machine has no address other than loopback, so a request cannot arrive as a stranger")
    server = Server(host="0.0.0.0")
    try:
        status, pairs, body = server.raw("GET", "/", connect_to=address)
        assert status == 403 and b"Not authorised" in body, (status, body[:60])
        assert values(pairs, "X-Frame-Options") == ["DENY"], pairs
        assert FRAME_POLICY in values(pairs, "Content-Security-Policy"), pairs
        token = (HOME_DIR / "lan-token").read_text().strip()
        status, pairs, _ = server.raw("GET", f"/?k={token}", connect_to=address)
        assert status == 200 and values(pairs, "Set-Cookie"), (status, pairs)
        assert values(pairs, "X-Frame-Options") == ["DENY"], pairs
        assert values(pairs, "Content-Security-Policy") == [FRAME_POLICY], pairs
    finally:
        server.stop()


# Every header serve.py sent on 14 Nov 2026 (HEAD 46ddd59, release 2.0.1), per kind
# of response, without Date and Server. A value that changes from one request to
# the next (a length, a modification time) is written "*". The shots' two
# Cache-Control headers are as they were; they are not part of this change.
NO_STORE = ("cache-control", "no-store, must-revalidate")
_PAGE = [NO_STORE, ("content-length", "*"), ("content-type", "text/html"), ("last-modified", "*")]
_REPORT = [("cache-control", "no-store"), ("content-length", "*"), ("content-type", "application/json; charset=utf-8")]
_JSON = [NO_STORE, ("content-length", "*"), ("content-type", "application/json; charset=utf-8")]
_ERROR = [NO_STORE, ("connection", "close"), ("content-length", "*"), ("content-type", "text/html;charset=utf-8")]
_ERROR_ON_REPORT = [("connection", "close"), ("content-length", "*"), ("content-type", "text/html;charset=utf-8")]
_REFUSED = [("content-length", "*"), ("content-type", "text/plain; charset=utf-8"), ("x-content-type-options", "nosniff")]


def _shot(kind: str) -> list[tuple[str, str]]:
    return [("cache-control", "private, max-age=300"), NO_STORE, ("content-length", "*"), ("content-type", kind),
            ("x-content-type-options", "nosniff")]


HEADERS_BEFORE = {
    "the page": _PAGE,
    "the page, as /index.html": _PAGE,
    "the page, head only": _PAGE,
    "the page, not modified": [NO_STORE],
    "report.json": _REPORT,
    "report.json with a query": _REPORT,
    "report.json, head only (not routed: 404)": _ERROR_ON_REPORT,
    "a picture under shots/": _shot("image/png"),
    "an svg under shots/": _shot("image/svg+xml"),
    "a picture that is not there": _ERROR,
    "a path that is not there": _ERROR,
    "a plain file of the folder": [NO_STORE, ("content-length", "*"), ("content-type", "text/plain"), ("last-modified", "*")],
    "a folder without its slash: a redirect": [NO_STORE, ("content-length", "*"), ("location", "/sub/")],
    "a folder's listing": [NO_STORE, ("content-length", "*"), ("content-type", "text/html; charset=utf-8")],
    "a read under another name: refused": _REFUSED,
    "a head under another name: refused": [*_REFUSED, NO_STORE],
    "an answer from the page: recorded": _JSON,
    "an answer from another origin: refused": _JSON,
    "an answer sent as text: refused": _JSON,
    "an answer that is not JSON: refused": _JSON,
    "a post to a path that does not take one": _ERROR,
    "OPTIONS: not supported": _ERROR,
    "PUT: not supported": _ERROR,
    "DELETE: not supported": _ERROR_ON_REPORT,
}


def test_F5_nothing_else_in_the_headers_changed():
    """Each response is what it was, plus the two protections and not one header more."""
    server = Server()
    faults: list[str] = []
    try:
        rows = kinds(server)
        assert {row[0] for row in rows} == set(HEADERS_BEFORE), "the table of headers and the list of responses differ"
        for label, status, method, path, headers, body in rows:
            _, pairs, _ = server.raw(method, path, headers, body)
            shot = path.startswith("/shot/") and status == 200
            policy = f"{SHOT_POLICY_BEFORE}; {FRAME_POLICY}" if shot else FRAME_POLICY
            wanted = sorted([*HEADERS_BEFORE[label], ("x-frame-options", "DENY"), ("content-security-policy", policy)])
            got = sorted((name, "*" if name in ("content-length", "last-modified") else value)
                         for name, value in pairs if name not in ("date", "server"))
            if got != wanted:
                added = [h for h in got if h not in wanted]
                missing = [h for h in wanted if h not in got]
                faults.append(f"{label}: missing {[n for n, _ in missing]}, not expected {[n for n, _ in added]}")
    finally:
        server.stop()
    assert not faults, f"{len(faults)} responses differ from `before + the two protections`, e.g. " + "; ".join(faults[:2])


def test_F6_no_response_lets_another_site_read_the_board():
    """No Access-Control-* header anywhere, and a preflight is refused: the board is not readable cross-origin."""
    server = Server()
    try:
        for label, _, method, path, headers, body in kinds(server):
            _, pairs, _ = server.raw(method, path, headers, body)
            leaked = [name for name, _ in pairs if name.startswith("access-control-") or name == "timing-allow-origin"]
            assert not leaked, f"{label}: {leaked}"
        preflight = {"Origin": "https://example.invalid", "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"}
        status, pairs, _ = server.raw("OPTIONS", "/answer", preflight)
        assert status == 501 and not [n for n, _ in pairs if n.startswith("access-control-")], (status, pairs)
    finally:
        server.stop()


def main() -> int:
    if ARGS.show:
        show()
        return 0
    setup()
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    failed = skipped = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"PASS  {name}")
            except Skip as why:
                skipped += 1
                print(f"SKIP  {name}: {why}")
            except Exception as exc:  # noqa: BLE001 - a test runner reports every failure
                failed += 1
                reason = " ".join(str(exc).split())[:260] or type(exc).__name__
                print(f"FAIL  {name}: {type(exc).__name__}: {reason}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed - skipped} passed, {failed} failed, {skipped} skipped, plugin under test: {PLUGIN}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
