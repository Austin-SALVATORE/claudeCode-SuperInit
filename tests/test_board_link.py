#!/usr/bin/env python3
"""The board's link in the session (release 2.2.0, part C, 15 Nov 2026).

    python3 test_board_link.py --plugin <checkout of the plugin>

The owner asked for the board's short address, `http://localhost:8800/<name>`, to be there to see and to click in a
session of a project that has a board. It is drawn by a mod (`hooks/board-link.tsx`, a row above the prompt with a
`Link`), which asks one helper, `status-board/board_link.py`, for the address or for nothing. The mod needs a terminal; the
helper does not, and every rule of the task is a rule of the helper, so these tests run it as the mod does (a process, a
folder the session was started in, an optional payload on stdin) and read what it prints: one line of JSON,
`{"state": <the home it read>, "url": <the address or null>, "up": <is the server's process alive, or null>}`, or nothing.

Every run has a temporary HOME and STATUS_BOARD_HOME, scratch git repositories, scratch records of boards that are
servers nobody started, and `STATUS_BOARD_FRONT_PORT` set to a port outside 8800 to 8920 (the home is not the account's, so
its boards take part in the front door's election only because that is given). No test binds a port from 8800 to 8920, and
none sends a request to anything: the two tests that listen do so to prove that the helper never connects.

Run it against the code before the change and every test but the ones about what the files must not do fails.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="checkout of the plugin to test")
ARGS = ap.parse_args()
PLUGIN = Path(ARGS.plugin).resolve()
HELPER = PLUGIN / "status-board" / "board_link.py"
MOD = PLUGIN / "hooks" / "board-link.tsx"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
sys.dont_write_bytecode = True

TMP = Path(tempfile.mkdtemp(prefix="board-link-")).resolve()
FAKE_HOME = TMP / "fakehome"
PROCESSES: list[subprocess.Popen] = []
SENTINEL = "SECRET-CONTENT-OF-A-BOARD"
FRONT_PORT = 0  # set by setup(): a free port, outside 8800 to 8920


class Skip(Exception):
    pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert not 8800 <= port <= 8920, port
    return port


def setup() -> None:
    global FRONT_PORT
    FAKE_HOME.mkdir(exist_ok=True)
    FRONT_PORT = free_port()


class World:
    """A temporary STATUS_BOARD_HOME, and the projects and boards in it."""

    def __init__(self, name: str = "w") -> None:
        self.root = Path(tempfile.mkdtemp(dir=TMP, prefix=name + "-"))
        self.state = self.root / "home"
        self.state.mkdir()
        self.live = subprocess.Popen(["sleep", "600"])
        PROCESSES.append(self.live)
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.dead_pid = dead.pid

    def project(self, name: str, git: bool = True) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        if git:
            git_init(path)
        return path

    def board(self, slug: str, repo: Path | None, record: bool = True, up: bool = True, port: int | None = None,
              name: str | None = None, repo_json: bool = True) -> Path:
        """A board folder (with its repo.json, narrative and answers: content the helper must never read) and, with
        `record`, the record its server wrote: alive (`up`) or the process of a server that is gone."""
        board = self.state / "boards" / slug
        board.mkdir(parents=True, exist_ok=True)
        if repo is not None and repo_json:
            (board / "repo.json").write_text(json.dumps({"repo": str(repo.resolve())}))
        (board / "narrative.json").write_text(json.dumps({"headline": SENTINEL}))
        (board / "answers.json").write_text(json.dumps({"Q?": {"answer": SENTINEL, "at": "2026-11-15T00:00:00+01:00"}}))
        if record and repo is not None:
            records = self.state / "servers"
            records.mkdir(mode=0o700, exist_ok=True)
            port = port or free_port()
            rec = {"v": 1, "service": "status-board", "slug": slug, "name": name or slug, "repo": str(repo.resolve()),
                   "main_repo": None, "port": port, "host": "127.0.0.1", "pid": self.live.pid if up else self.dead_pid,
                   "started": "2026-11-15T00:00:00+01:00", "plugin_version": "2.2.0"}
            (records / f"{port}.json").write_text(json.dumps(rec))
        return board

    def env(self, **extra: str) -> dict:
        env = {"HOME": str(FAKE_HOME), "PATH": f"/usr/bin:/bin:{os.path.dirname(sys.executable)}",
               "STATUS_BOARD_HOME": str(self.state), "STATUS_BOARD_FRONT_PORT": str(FRONT_PORT),
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return env


def git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)


def run_helper(world: World, start: Path | str, payload: dict | None = None, env_extra: dict | None = None,
               timeout: float = 20.0) -> tuple[subprocess.CompletedProcess, dict | None]:
    """The helper as the mod runs it: `python3 board_link.py --start <folder>`, a payload on stdin; its one line parsed."""
    done = subprocess.run([sys.executable, "-B", str(HELPER), "--start", str(start)], input=json.dumps(payload or {}),
                          env=world.env(**(env_extra or {})), capture_output=True, text=True, timeout=timeout, cwd=str(TMP))
    out = done.stdout.strip()
    return done, (json.loads(out) if out else None)


def address(slug: str) -> str:
    return f"http://localhost:{FRONT_PORT}/{slug}"


# ── point 3: what is shown, and what is not ────────────────────────────────────────────────────────────────────────
def test_a_project_with_a_board_that_is_running_shows_its_address_by_name():
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    done, said = run_helper(world, repo)
    assert done.returncode == 0 and said is not None, (done.returncode, done.stdout, done.stderr)
    assert said["url"] == address("shop-app") and said["up"] is True, said
    assert said["state"] == str(world.state), said


def test_a_board_whose_server_is_down_keeps_its_address_marked_not_running():
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo, up=False)
    _, said = run_helper(world, repo)
    assert said and said["url"] == address("shop-app") and said["up"] is False, said


def test_a_project_with_no_board_shows_nothing_and_says_where_to_look_for_one_later():
    world = World()
    repo = world.project("notes")
    _, said = run_helper(world, repo)
    assert said is not None and said["url"] is None and said["state"] == str(world.state), said


def test_a_folder_that_is_no_project_shows_nothing():
    world = World()
    world.board("shop-app", world.project("shop-app"))
    plain = world.project("plain", git=False)
    for place in (plain, FAKE_HOME, Path("/"), Path("/tmp")):
        _, said = run_helper(world, place)
        assert said is None or said["url"] is None, (place, said)


def test_a_board_that_has_never_been_started_has_no_address_yet_and_shows_nothing():
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo, record=False)
    _, said = run_helper(world, repo)
    assert said is not None and said["url"] is None, said


def test_a_worker_a_teammate_a_subagent_see_nothing():
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    for extra in ({"agent_id": "a1"}, {"agent_type": "general-purpose"}, {"teammate_name": "t1"}, {"team_name": "t"}):
        _, said = run_helper(world, repo, payload={"session_id": "s", **extra})
        assert said is None or said["url"] is None, (extra, said)
    # an Orca worker: its first prompt opens with the preamble Orca writes (the evidence orca_session.py asks first)
    transcript = world.root / "worker.jsonl"
    preamble = ("Your coordinator's terminal handle is: term_00000000-1111-2222-3333-444444444444 "
                "Your task ID is: task_0123456789ab")
    transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": preamble}}, separators=(",", ":")) + "\n")
    in_orca = {"ORCA_TERMINAL_HANDLE": "term_00000000-1111-2222-3333-444444444444", "ORCA_CLI_COMMAND": "/nonexistent/orca-for-tests"}
    _, said = run_helper(world, repo, payload={"session_id": "s", "transcript_path": str(transcript)}, env_extra=in_orca)
    assert said is None or said["url"] is None, said
    _, said = run_helper(world, repo, payload={"session_id": "s"}, env_extra=in_orca)
    assert said and said["url"], "the same session without that evidence is the owner's (and no real Orca is asked: the CLI is not there)"


# ── point 2: the project, the board and the name are found the way 2.1.0 finds them ─────────────────────────────────
def test_the_session_started_in_a_subfolder_of_the_project_is_the_projects():
    world = World()
    repo = world.project("shop-app")
    (repo / "src" / "deep").mkdir(parents=True)
    world.board("shop-app", repo)
    _, said = run_helper(world, repo / "src" / "deep")
    assert said and said["url"] == address("shop-app"), said


def test_a_sub_project_with_an_old_board_is_not_taken_for_the_roots_board():
    """The rules of answer_ledger.find_board hold here too: an old board (no repo.json) below the top of a repository is
    in doubt, and the link must not send the owner to a board the session is not told the answers of."""
    world = World()
    root = world.project("shop-app")
    (root / "sub").mkdir()
    world.board("shop-app", root)
    sub_board = world.board("sub", root / "sub", repo_json=False)
    assert not (sub_board / "repo.json").exists()
    _, said = run_helper(world, root / "sub")
    assert said is None or said["url"] is None, said


def test_the_name_must_be_one_the_front_door_sends_to_this_projects_board():
    """Another project holds the name, with a running server: the front door would send /shop-app there (or show a
    clash page), not to this project's board; a link that does not lead to this project is worse than none."""
    world = World()
    mine = world.project("shop-app")
    other = world.root / "elsewhere" / "shop-app"
    git_init(other)
    world.board("shop-app", mine, up=False)
    world.board("shop-app", other, up=True)  # a second record for the same slug, from another project, alive
    _, said = run_helper(world, mine)
    assert said is None or said["url"] is None, said


def test_a_board_found_in_a_home_whose_boards_are_not_behind_the_front_door_shows_nothing():
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    env = world.env()
    del env["STATUS_BOARD_FRONT_PORT"]  # not the account's home and no port given: its boards do not take part
    done = subprocess.run([sys.executable, "-B", str(HELPER), "--start", str(repo)], input="{}", env=env,
                          capture_output=True, text=True, timeout=20, cwd=str(TMP))
    out = done.stdout.strip()
    said = json.loads(out) if out else None
    assert said is None or said["url"] is None, said


def test_the_address_is_the_one_the_front_door_builds():
    """Reused, not copied: the helper's address is front_door.name_address for the board's name (quoted as it is)."""
    world = World()
    repo = world.project("Shop App")  # a folder with a capital and a space: the slug is `shop-app`
    world.board("shop-app", repo)
    _, said = run_helper(world, repo)
    assert said and said["url"] == address("shop-app"), said


# ── point 5: it costs nothing and can't hurt ────────────────────────────────────────────────────────────────────────
def tree_fingerprint(*roots: Path) -> dict:
    out = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            try:
                st = path.stat()
            except OSError:
                continue
            out[str(path)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def test_it_writes_nothing():
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    before = tree_fingerprint(world.state, repo, FAKE_HOME, PLUGIN / "status-board", PLUGIN / "hooks")
    run_helper(world, repo)
    run_helper(world, repo / "nowhere")
    after = tree_fingerprint(world.state, repo, FAKE_HOME, PLUGIN / "status-board", PLUGIN / "hooks")
    assert before == after, sorted(set(after) ^ set(before))[:5] or [k for k in after if before.get(k) != after[k]][:5]


def test_it_never_connects_to_a_board_or_to_the_front_door():
    world = World()
    repo = world.project("shop-app")
    board_port, door_port = free_port(), FRONT_PORT
    world.board("shop-app", repo, up=True, port=board_port)
    listeners = []
    for port in (board_port, door_port):
        sock = socket.socket()
        sock.bind(("127.0.0.1", port))
        sock.listen(5)
        sock.settimeout(0.2)
        listeners.append(sock)
    try:
        _, said = run_helper(world, repo)
        assert said and said["url"] == address("shop-app"), said
        for sock in listeners:
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            conn.close()
            raise AssertionError(f"something connected to {sock.getsockname()}: the helper asked a board or the front door")
    finally:
        for sock in listeners:
            sock.close()


def test_it_never_opens_what_a_board_says():
    """Run in this process with an audit hook on `open`: the helper may read repo.json, the records and the names of the
    folders of boards, and never answers.json, narrative.json or any other file of a board's content."""
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    code = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {str(PLUGIN / 'status-board')!r})\n"
        f"sys.argv = ['board_link.py', '--start', {str(repo)!r}]\n"
        "sys.dont_write_bytecode = True\n"
        "import runpy\n"
        "def hook(event, args):\n"
        "    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):\n"
        "        sys.stderr.write('OPEN ' + os.fsdecode(args[0]) + ' ' + str(args[1]) + ' ' + str(args[2]) + '\\n')\n"
        "sys.addaudithook(hook)\n"
        "runpy.run_path(" + repr(str(HELPER)) + ", run_name='__main__')\n"
    )
    done = subprocess.run([sys.executable, "-B", "-c", code], input="{}", env=world.env(), capture_output=True, text=True,
                          timeout=20, cwd=str(TMP))
    seen = [line.split(" ", 1)[1] for line in done.stderr.splitlines() if line.startswith("OPEN ")]
    assert any("repo.json" in line or "servers" in line for line in seen), f"the audit saw nothing of the records: {seen[:5]} {done.stderr[-300:]}"
    forbidden = ("answers.json", "narrative.json", ".surfaced-to-agent.json", "keeper.json", "tree-lock.json")
    bad = [line for line in seen if any(name in line for name in forbidden)]
    assert not bad, f"the helper opened content of a board: {bad}"
    writes = []
    for line in seen:
        path, mode, _flags = line.rsplit(" ", 2)
        if str(TMP) in path and mode != "None" and any(c in mode for c in "wax+"):
            writes.append(line)
    assert not writes, f"the helper opened something for writing: {writes}"
    assert SENTINEL not in done.stdout + done.stderr


def test_any_error_draws_nothing_and_exits_0_fast():
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    cases = [
        ("a start folder that does not exist", dict(start=TMP / "nope")),
        ("a payload that is not JSON", dict(raw="{not json")),
        ("no start folder", dict(no_start=True)),
        ("an unreadable state home", dict(env={"STATUS_BOARD_HOME": str(TMP / "no-such-home")})),
        ("a record that is not JSON", dict(bad_record=True)),
    ]
    for label, case in cases:
        if case.get("bad_record"):
            (world.state / "servers").mkdir(mode=0o700, exist_ok=True)
            (world.state / "servers" / "1.json").write_text("{not json")
        argv = [sys.executable, "-B", str(HELPER)]
        if not case.get("no_start"):
            argv += ["--start", str(case.get("start", repo))]
        started = time.time()
        done = subprocess.run(argv, input=case.get("raw", "{}"), env=world.env(**case.get("env", {})), capture_output=True,
                              text=True, timeout=20, cwd=str(TMP))
        took = time.time() - started
        assert done.returncode == 0, (label, done.returncode, done.stderr[-300:])
        assert done.stderr == "", (label, done.stderr[-300:])
        assert took < 5, (label, took)
        out = done.stdout.strip()
        assert out == "" or json.loads(out)["url"] in (None, address("shop-app")), (label, out)
        if label in ("a start folder that does not exist", "an unreadable state home"):
            assert out == "" or json.loads(out)["url"] is None, (label, out)


def test_one_run_is_fast():
    world = World()
    repo = world.project("shop-app")
    for i in range(20):
        world.board(f"proj-{i}", world.project(f"proj-{i}"))
    world.board("shop-app", repo)
    started = time.time()
    _, said = run_helper(world, repo)
    took = time.time() - started
    assert said and said["url"], said
    assert took < 2.0, f"one run took {took:.2f} s: it runs in every interactive session"


# ── the mod, read as text (it needs a terminal to run) and the plugin's declaration of it ───────────────────────────
def test_the_mod_is_declared_once_and_the_command_hooks_are_still_there():
    data = json.loads(HOOKS_JSON.read_text())
    assert data.get("modules") == ["./board-link.tsx"], data.get("modules")
    assert MOD.is_file(), "hooks/board-link.tsx does not exist"
    hooks = data.get("hooks", {})
    for event in ("UserPromptSubmit", "SessionStart"):
        assert hooks.get(event), f"the {event} command hooks are gone: {sorted(hooks)}"
    commands = " ".join(h["command"] for groups in hooks.values() for g in groups for h in g["hooks"])
    for script in ("board-answers.py", "answer-waiter.py", "revive-board.py", "offer-bootstrap.py"):
        assert script in commands, f"{script} is no longer hooked"


def test_the_mod_asks_the_helper_and_nothing_else():
    text = MOD.read_text()
    assert "board_link.py" in text, "the mod does not call the helper"
    for forbidden in ("$.http", "fetch(", "XMLHttpRequest", "$.fs.write", "answers.json", "narrative.json", "$.tool.call",
                      "$.agent", "$.model", "$.network"):
        assert forbidden not in text, f"the mod uses {forbidden}"
    assert "AbovePrompt" in text and "Link" in text, "it must draw a row above the prompt with a Link"
    assert "$.ui.status" not in text, "the status line carries a warning sign and the plugin's name: the form is the band"
    assert re.search(r"isInteractive", text), "it must draw nothing where nobody is at the prompt"


def test_the_mod_polls_slowly_and_starts_a_process_only_when_something_changed():
    """The owner has about ten sessions open: a process every few seconds in each is too much. The mod looks at two folders
    with $.fs.stat (no process) every POLL_MS and starts the helper at the start, when one of them changed, and at most
    every REFRESH_MS otherwise (a server that died changes no file)."""
    text = MOD.read_text()
    poll = re.search(r"POLL_MS\s*=\s*([0-9_]+)", text)
    refresh = re.search(r"REFRESH_MS\s*=\s*([0-9_]+)", text)
    assert poll and refresh, "POLL_MS and REFRESH_MS are not named"
    poll_ms, refresh_ms = int(poll.group(1).replace("_", "")), int(refresh.group(1).replace("_", ""))
    assert poll_ms >= 10_000, f"the folders are looked at every {poll_ms} ms"
    assert refresh_ms >= 120_000, f"the helper is started every {refresh_ms} ms whatever happened"
    assert "$.fs.stat" in text and "mtimeMs" in text, "the change is read from the folders' times, not by starting a process"
    assert text.count("$.process.run") == 1, "one place starts the helper"


def test_the_readme_says_what_it_shows_where_and_what_it_needs():
    text = (PLUGIN / "README.md").read_text()
    for word in ("board-link", "2.1.287", "above the prompt", "OSC 8", "(not running)"):
        assert word in text, f"README.md does not say {word}"
    assert not re.search(r"/Users/|/home/|claude-(?:opus|sonnet|haiku|fable)-\d", text)


def test_a_helper_that_said_nothing_at_the_first_run_is_not_started_at_every_poll():
    """After the check of 15 Nov (finding 3): `state` stays null when the first run prints nothing (no python3 on the
    PATH, a broken folder), and a condition that starts the helper whenever it is null starts it every POLL_MS for the whole
    session. A run that said nothing waits REFRESH_MS like any quiet run."""
    text = MOD.read_text()
    condition = re.search(r"if \(([^)]*isDue[^)]*)\)", text)
    assert condition, "the poll's condition (isChanged, isDue) is not there"
    assert "state === null" not in condition.group(1), f"a helper that said nothing is retried at every poll: {condition.group(1)}"
    assert "isChanged" in condition.group(1) and "isDue" in condition.group(1)


def test_a_name_that_needs_escaping_in_a_url_gives_the_address_the_front_door_unquotes_to_the_boards_name():
    """Finding 5: the door unquotes the path of `GET /<name>`; the link must say the same name, quoted once."""
    from urllib.parse import quote, unquote

    world = World()
    for folder in ("My App", "notes#2", "a?b", "50%off", "caf\u00e9", "x&y"):
        repo = world.project(folder)
        slug = folder.lower().replace(" ", "-")
        world.board(slug, repo)
        _, said = run_helper(world, repo)
        assert said and said["url"] == f"http://localhost:{FRONT_PORT}/{quote(slug)}", (folder, said)
        path = said["url"].split(f":{FRONT_PORT}/", 1)[1]
        assert unquote(path) == slug and "?" not in path and "#" not in path, (folder, path)


def test_a_record_whose_pid_is_reused_shows_the_link_unmarked_and_never_another_projects_board():
    """Finding 2/5, pinned as it is: `up` is 'the record's process is alive', so a pid that another program now has keeps the
    link without `(not running)`. The link still leads to the door, which says what is there; it is never another board."""
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo, up=True)  # the record's pid is a live `sleep`: not a board's server
    _, said = run_helper(world, repo)
    assert said and said["url"] == address("shop-app") and said["up"] is True, said
    other = world.project("other-app")
    world.board("other-app", other, up=True)
    _, again = run_helper(world, repo)
    assert again and again["url"] == address("shop-app"), again


def test_the_readme_says_not_running_can_be_late_and_what_port_8800_means():
    text = " ".join((PLUGIN / "README.md").read_text().split())
    assert "within five minutes" in text or "up to five minutes" in text, "README.md does not say that (not running) can be late"
    assert re.search(r"another program holds port 8800", text), "README.md does not say what the link does when 8800 is another program's"
    assert "start line" in text and "port address" in text, "README.md does not say the board's own port address still works"


def test_the_status_board_skill_says_the_row_is_there_and_to_give_the_address_once():
    """The check's sentence (finding 1), word for word, once, in the paragraph that tells the agent to give the address."""
    text = (PLUGIN / "skills" / "status-board" / "SKILL.md").read_text()
    sentence = ("The board's short address is also shown in a row above the prompt (Claude Code 2.1.287 or later), which you cannot see. "
                "Give the address once, when you start or first mention the board, and don't repeat it in later replies unless the owner asks.")
    assert text.count(sentence) == 1, f"the sentence is there {text.count(sentence)} times"
    paragraph = text[text.index("**Give the owner the address by name, not the port.**"):]
    paragraph = paragraph[:paragraph.index("\n\n")]
    assert paragraph.rstrip().endswith(sentence), "it must be the last sentence of that paragraph"
    assert " he " not in sentence and "his" not in sentence.split()


def test_the_row_sits_at_the_right_end_by_layout_and_never_by_padding_with_spaces():
    """The owner's ask of 15 Nov (13:52): the content at the right end of the row, beside the engine's [-]. By the Box's own
    `justifyContent`, which follows the width of the terminal (a resize moves it); never by counting columns and padding."""
    text = MOD.read_text()
    assert 'justifyContent="flex-end"' in text, "the row's content is not laid out at the end of its row"
    for guess in ("bodyColumns", "viewport", ".repeat(", "padStart", "padEnd", "' '.repeat", '" ".repeat'):
        assert guess not in text, f"the mod guesses the width of the terminal: {guess}"
    assert not re.search(r"<Text[^>]*>[ ]{3,}\S", text), "padding with spaces inside a Text"


def test_a_name_too_long_for_the_row_is_cut_with_an_ellipsis_and_keeps_one_row():
    """Never a second row, never the engine's [-] pushed away: the one Text that holds the address is truncated at the end
    (the link keeps the whole address: the cut is of what is drawn)."""
    text = MOD.read_text()
    assert 'wrap="truncate-end"' in text, "the row does not truncate"
    assert re.search(r'<Text wrap="truncate-end">\s*<Text dimColor>board </Text>\s*<Link href=\{shown\.url\} />', text), \
        "the label, the Link and the marker are not one truncated Text"


def test_the_row_does_not_replace_the_rows_of_other_mods():
    """Before Claude Code 2.1.323 all mods shared one drawing in the band, and a tree without `await next(e)` in it replaced
    what the mods after it drew (the docs: interface.md, 'Band above the prompt', and troubleshoot.md, 'Another mod's band shows
    and yours doesn't'). The result of `await next(e)` is among the children of a Box, and no Box of the row carries a prop
    that makes the tree invalid there: width, height, minWidth, minHeight, position, top, left, right, bottom, display, overflow."""
    text = MOD.read_text()
    assert re.search(r"const theirs = await next\(e\)", text), "the row does not keep what the mods after it draw"
    assert re.search(r"<Box flexDirection=\"column\">[\s\S]*\{theirs\}[\s\S]*</Box>", text), "`theirs` is not a child of a Box"
    for box in re.findall(r"<Box\b[^>]*>", text):
        bad = re.findall(r"\b(width|height|minWidth|minHeight|position|top|left|right|bottom|display|overflow)\s*=", box)
        assert not bad, f"{box} carries {bad}, which makes the tree invalid before 2.1.323"
    assert "return next(e)" in text, "with nothing to show the row must still pass the event on"


def test_the_readme_says_where_the_row_sits_and_what_a_narrow_terminal_does():
    text = " ".join((PLUGIN / "README.md").read_text().split())
    assert re.search(r"at the right end of the row, beside the `\[-\]`", text), "README.md does not say the content is at the right end"
    assert "ellipsis" in text and "full address" in text, "README.md does not say what a name too long for the row does"
    assert "2.1.323" in text and "other mods" in text, "README.md does not say the row leaves other mods' rows alone"


# ── release 2.3.0, part A (15 Nov 2026): where the link shows is a choice, made at set-up ──────────────────────────────
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"
SKILL_LINK = PLUGIN / "skills" / "board-link" / "SKILL.md"
SEEN_SECONDS = 600  # the link counts as shown in the status line for ten minutes after the filter last placed it


def seen_name(start: Path | str) -> str:
    return hashlib.sha1(str(start).encode()).hexdigest()[:16]


def test_the_manifest_declares_the_two_switches_as_plain_booleans():
    """Claude Code's `userConfig`: the enable dialog, the /config rows, `pluginConfigs` in the user's settings. Two booleans,
    never a field with `options`: 'users on Claude Code versions before v2.1.271 can't load the plugin' that declares one
    (plugins-reference.md, "Limit a field to fixed options"), and that would lock them out of every hook too."""
    config = json.loads(MANIFEST.read_text()).get("userConfig")
    assert isinstance(config, dict) and set(config) == {"board_link_row", "board_link_status_line"}, config
    row, line = config["board_link_row"], config["board_link_status_line"]
    assert row["type"] == "boolean" and row["title"] == "Board link above the prompt" and row["default"] is True, row
    assert line["type"] == "boolean" and line["title"] == "Board link in the status line" and line["default"] is False, line
    for field in (row, line):
        assert field.get("description", "").strip(), field
        assert "options" not in field and not field.get("required") and not field.get("sensitive"), field


def test_helper_reports_seen_from_data_folder():
    """The filter touches `seen/<first 16 hex of sha1 of the start folder's path>` in the plugin's data folder each time it
    places the link; the helper says whether that file is younger than ten minutes, and where the data folder is."""
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    data = TMP / "data-seen"
    env = {"CLAUDE_PLUGIN_DATA": str(data)}
    _, said = run_helper(world, repo, env_extra=env)
    assert said and said["data"] == str(data) and said["status_line_seen"] is False, said  # no folder, no file: never seen
    (data / "seen").mkdir(parents=True)
    marker = data / "seen" / seen_name(repo)
    marker.write_text("")
    _, said = run_helper(world, repo, env_extra=env)
    assert said["status_line_seen"] is True and said["url"] == address("shop-app") and said["up"] is True, said
    assert str(marker) in said["seen_paths"], said
    old = time.time() - SEEN_SECONDS - 30
    os.utime(marker, (old, old))
    _, said = run_helper(world, repo, env_extra=env)
    assert said["status_line_seen"] is False, said
    other = data / "seen" / seen_name(repo / "elsewhere")  # another project's marker says nothing about this one
    other.write_text("")
    _, said = run_helper(world, repo, env_extra=env)
    assert said["status_line_seen"] is False, said


def test_helper_counts_the_marker_of_the_path_as_given_and_of_the_resolved_one():
    """A start folder reached through a link (`/tmp` is `/private/tmp` on a Mac): the filter may hash either form of the
    path, the helper looks at both."""
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    link = world.root / "linked-shop-app"
    link.symlink_to(repo, target_is_directory=True)
    data = TMP / "data-links"
    (data / "seen").mkdir(parents=True)
    env = {"CLAUDE_PLUGIN_DATA": str(data)}
    (data / "seen" / seen_name(link)).write_text("")
    _, said = run_helper(world, link, env_extra=env)
    assert said and said["status_line_seen"] is True, said
    (data / "seen" / seen_name(link)).unlink()
    (data / "seen" / seen_name(repo.resolve())).write_text("")
    _, said = run_helper(world, link, env_extra=env)
    assert said and said["status_line_seen"] is True, said


def test_helper_works_out_the_data_folder_from_its_own_path_when_the_mod_cannot_tell_it():
    """A mod and the process it starts are not given CLAUDE_PLUGIN_DATA (spike of 15 Nov, SPIKE.md section 5); a command hook is.
    Installed: <plugins>/cache/<marketplace>/<name>/<version> gives <plugins>/data/<name>-<marketplace>. Loaded by
    --plugin-dir: <config>/plugins/data/<name>-inline, the config folder being CLAUDE_CONFIG_DIR or ~/.claude."""
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    plugins = TMP / "cfg-installed" / "plugins"
    root = plugins / "cache" / "somemarket" / "project-init" / "9.9.9"
    for part in ("status-board", "hooks", ".claude-plugin"):
        shutil.copytree(PLUGIN / part, root / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    done = subprocess.run([sys.executable, "-B", str(root / "status-board" / "board_link.py"), "--start", str(repo)], input="{}",
                          env=world.env(), capture_output=True, text=True, timeout=20, cwd=str(TMP))
    said = json.loads(done.stdout.strip())
    assert said["data"] == str(plugins / "data" / "project-init-somemarket"), said
    config = TMP / "cfg-inline"
    done = subprocess.run([sys.executable, "-B", str(HELPER), "--start", str(repo)], input="{}",
                          env=world.env(CLAUDE_CONFIG_DIR=str(config)), capture_output=True, text=True, timeout=20, cwd=str(TMP))
    said = json.loads(done.stdout.strip())
    assert said["data"] == str(config / "plugins" / "data" / "project-init-inline"), said
    done = subprocess.run([sys.executable, "-B", str(HELPER), "--start", str(repo)], input="{}",
                          env=world.env(), capture_output=True, text=True, timeout=20, cwd=str(TMP))
    said = json.loads(done.stdout.strip())
    assert said["data"] == str(FAKE_HOME / ".claude" / "plugins" / "data" / "project-init-inline"), said
    assert not (FAKE_HOME / ".claude" / "plugins" / "data").exists(), "the helper made the data folder: it only reads"


def test_helper_keeps_the_keys_the_status_line_calls_and_its_command_line():
    """Status line filters, the shipped one and hand-made ones (a script named hud-board-link.py that some set-ups pipe their status
    line through), run `board_link.py --start <dir> --no-agent-check` and read `url` and `up` from the one line of JSON: a contract.
    New keys are added; none is renamed or dropped."""
    world = World()
    repo = world.project("shop-app")
    world.board("shop-app", repo)
    done = subprocess.run([sys.executable, "-B", str(HELPER), "--start", str(repo), "--no-agent-check"], stdin=subprocess.DEVNULL,
                          env=world.env(), capture_output=True, text=True, timeout=20, cwd=str(TMP))
    said = json.loads(done.stdout.strip())
    assert said["url"] == address("shop-app") and said["up"] is True and said["state"] == str(world.state), said
    assert done.stdout.count("\n") == 1, "one line of JSON"


def test_the_mod_reads_the_two_switches_once_and_applies_the_rule_in_one_place():
    text = MOD.read_text()
    assert re.search(r"register: Register = \(\s*on\s*,\s*options\s*\)", text), "register must take the options"
    assert "board_link_row" in text and "board_link_status_line" in text, "the mod does not read the two switches"
    assert re.search(r"SEEN_MS\s*=\s*600_000", text), "the status line counts as showing the link for ten minutes"
    rule = re.search(r"const isWanted = \(\)[^\n]*=>[^\n]*", text)
    assert rule, "the rule that decides whether the row is drawn is not one named function"
    body = text[text.index("const isWanted"):]
    body = body[: body.index("\n\n")] if "\n\n" in body else body
    assert "wantsRow" in body and "wantsStatusLine" in body and "isSeen" in body, body
    assert re.search(r"if \(shown === null \|\| e\.props\.hasSurvey \|\| !isWanted\(\)\)", text), "ui.render must stop at the rule"


def test_the_mod_learns_a_change_of_the_switches_from_the_engines_reload_not_from_its_own_hook_or_poll():
    """Spike of 15 Nov (SPIKE.md): the engine reloads the mod, `register(on, options)` runs again, whenever the plugin's options
    change, from /config or in the settings file. A `config.set` hook or a re-read of `pluginConfigs` would add nothing."""
    text = MOD.read_text()
    assert "'config.set'" not in text and "$.settings" not in text, "the mod listens for what the reload already tells it"


def test_the_mod_looks_for_the_status_line_marker_without_starting_a_process():
    text = MOD.read_text()
    assert "seen_paths" in text and "status_line_seen" in text, "the mod does not read what the helper says about the marker"
    assert text.count("$.process.run") == 1, "one place starts the helper"
    assert "$.fs.stat" in text and "mtimeMs" in text


def test_claude_plugin_test_runs_the_mods_own_cases():
    """The rule of the row, run by Claude Code's own kit (`claude plugin test`): the mod is loaded with given `options`, the
    helper's answer and the marker's time are answered below it, the clock is moved by the test."""
    claude = shutil.which("claude")
    if not claude:
        raise Skip("claude is not on PATH")
    cases = PLUGIN / "tests" / "board-link.test.tsx"
    assert cases.is_file(), "tests/board-link.test.tsx does not exist"
    done = subprocess.run([claude, "plugin", "test", str(PLUGIN)], capture_output=True, text=True, timeout=180,
                          env={"HOME": str(FAKE_HOME), "PATH": os.environ.get("PATH", "")}, cwd=str(TMP))
    out = done.stdout + done.stderr
    assert done.returncode == 0 and re.search(r"\b0 fail\b", out), out[-1500:]
    passed = re.search(r"(\d+) pass\b", out)
    assert passed and int(passed.group(1)) >= 8, out[-800:]


def test_the_readme_names_the_two_switches_and_says_nothing_depends_on_claude_hud():
    text = " ".join((PLUGIN / "README.md").read_text().split())
    for word in ("Board link above the prompt", "Board link in the status line", "/config", "/project-init:board-link", "claude-hud"):
        assert word in text, f"README.md does not say {word}"
    assert re.search(r"works without claude-hud|no dependency on claude-hud|does not need claude-hud", text), \
        "README.md does not say the plugin works without claude-hud"
    assert re.search(r"statusline_setup\.py\"? remove|Take the link out of the status line", text), "README.md does not say how the wrapper is taken out"
    assert "within ten minutes" in text or "within 10 minutes" in text, "README.md does not say the row comes back by itself"
    assert not re.search(r"/Users/|/home/|/opt/homebrew", text)


def test_the_board_link_skill_asks_one_question_with_three_answers():
    text = SKILL_LINK.read_text()
    head = text.split("---", 2)[1]
    assert re.search(r"^name:\s*board-link\s*$", head, re.M) and re.search(r"^description:", head, re.M), head
    assert "Where should the board's link show?" in text and "Board link" in text
    for answer in ("Above the prompt", "In the status line", "Both"):
        assert answer in text, f"the skill does not offer {answer}"
    assert "AskUserQuestion" in text


def test_the_skill_names_claude_huds_commands_verbatim_and_never_installs_it():
    text = SKILL_LINK.read_text()
    for command in ("/plugin marketplace add jarrodwatts/claude-hud", "/plugin install claude-hud", "/reload-plugins", "/hud"):
        assert command in text, f"the skill does not give {command}"
    assert re.search(r"type these (?:commands )?yourself", text), "the skill must say the owner types them"
    assert re.search(r"does not (?:install|run)[^.]*claude-hud|never (?:install|run)[^.]*claude-hud", text, re.I), \
        "the skill must say it does not install another author's plugin"
    for words in ("Not now", "Don't suggest claude-hud again"):
        assert words in text, words


def test_the_skill_never_runs_add_without_a_yes_step():
    """`statusline_setup.py add` is the one thing that edits a user's settings.json: it appears, as something to run, only
    after the step in which the owner has been shown the old and the new command and has said yes."""
    text = SKILL_LINK.read_text()
    marker = "## After the owner's yes"
    assert marker in text, "the skill has no step named after the owner's yes"
    # `add --dry-run` writes nothing and is how the owner is shown the old and the new command (and learns of a refusal) before
    # the owner is asked; any other `add` is the edit itself
    commands = [m.start() for m in re.finditer(r"statusline_setup\.py[\"']?\s+add\b(?![^\n`]*--dry-run)", text)]
    assert commands, "the skill never says how the edit is made"
    assert all(pos > text.index(marker) for pos in commands), "`add` is mentioned before the owner's yes"
    assert re.search(r"statusline_setup\.py[\"']?\s+add\b[^\n`]*--dry-run", before_of(text, marker)), "the owner is not shown the commands by a dry run"
    before = text[: text.index(marker)]
    assert re.search(r"show[^.]*(?:old|current)[^.]*new command|old and the new command", before, re.I), \
        "the skill must show the owner the old and the new command before asking"
    assert re.search(r"(?i)never[^.]*\badd\b[^.]*without|do not run[^.]*\badd\b[^.]*before", text), "the rule is not said in words"


def before_of(text: str, marker: str) -> str:
    return text[: text.index(marker)]


def test_the_skill_sets_the_switches_with_claude_plugin_configure_and_never_edits_settings_itself():
    text = SKILL_LINK.read_text()
    assert "claude plugin configure project-init@claudecode-superinit --values-stdin" in text
    assert '"board_link_row"' in text and '"board_link_status_line"' in text
    assert re.search(r"(?i)never (?:edit|write)[^.\n]*settings", text), "the skill must say that it does not edit the settings file itself"
    assert "hud install" in text and re.search(r"(?i)never[^.\n]*hud install", text)


def test_the_skill_calls_the_setup_script_only_as_the_interface_fixes_it():
    """option/INTERFACE.md: `status`; `add [--replace-hand-made] [--dry-run]`; `remove [--dry-run]`; `record --answer
    row|status-line|both|declined [--hud proposed|declined]`. One line of JSON each; exit 0 done, 2 refused, 1 unexpected."""
    text = SKILL_LINK.read_text()
    calls = re.findall(r"statusline_setup\.py[\"']?\s+([a-z-]+)([^\n`]*)", text)
    assert calls, "the skill never calls the set-up script"
    allowed = {"status": set(), "add": {"--replace-hand-made", "--dry-run"}, "remove": {"--dry-run"}, "record": {"--answer", "--hud"}}
    for command, rest in calls:
        assert command in allowed, f"the skill calls a command the script does not have: {command}"
        flags = set(re.findall(r"--[a-z-]+", rest))
        assert flags <= allowed[command], (command, flags - allowed[command])
    answers = set(re.findall(r"--answer\s+([a-z-]+)", text))
    assert answers and answers <= {"row", "status-line", "both", "declined"}, answers
    assert set(re.findall(r"--hud\s+([a-z-]+)", text)) <= {"proposed", "declined"}
    for command in ("status", "add", "remove", "record"):
        assert any(c == command for c, _ in calls), f"the skill never uses {command}"
    assert re.search(r"exit (?:code )?2", text) and "refused" in text, "the skill must say what exit 2 and `refused` mean"


def test_the_skill_says_the_edit_can_be_refused_and_what_then():
    text = " ".join(SKILL_LINK.read_text().split())
    assert "refus" in text and "row above the prompt stays" in text
    assert "--replace-hand-made" in text and "Take the link out of the status line" in text


def test_the_skill_records_the_answer_and_is_asked_at_most_once():
    text = " ".join(SKILL_LINK.read_text().split())
    assert "statusline_setup.py" in text and re.search(r"\brecord\b", text)
    assert re.search(r"at most once|asked once|once per user", text)


def test_the_skill_shows_old_and_new_command_before_replacing_a_hand_made_filter_and_before_any_refusal_stop():
    """The independent check of 2.3.0, finding 1: for a status line that already pipes through a hand-made board filter from before 2.3.0,
    `add --dry-run` answers `refused` (code `hand-made`) and the steps collided: the refusal bullet said stop, and the
    replacement was run on a yes to a description. The hand-made case comes first: `add --replace-hand-made --dry-run`, the old
    and the new command shown, "Make this edit?", and only after the yes `add --replace-hand-made`."""
    text = SKILL_LINK.read_text()
    marker = "## After the owner's yes"
    before = text[: text.index(marker)]
    after = text[text.index(marker):]
    dry = before.find("--replace-hand-made --dry-run")
    assert dry >= 0, "the skill never runs `add --replace-hand-made --dry-run` before the yes"
    assert re.search(r"hand_made_filter[^\n]*true|`hand_made_filter`", before), "the hand-made case is not named"
    refusal = before.index("If a dry run says `refused`")
    assert dry < refusal, "the hand-made case must come before the bullet that stops on a refusal"
    case = before[dry - 400: dry + 900]
    assert re.search(r"old and the new command|old and new", case) and "code block" in case and "Make this edit?" in case, case
    assert re.search(r"hand-made", before[refusal: refusal + 700]), "the refusal bullet must say that the hand-made refusal is handled apart"
    # every mention of replacing before the yes is the dry run: the edit itself is only after it
    assert not re.search(r"--replace-hand-made(?![^\n`]*--dry-run)", before), "`--replace-hand-made` without the dry run before the yes"
    assert re.search(r"add\b[^\n]*--replace-hand-made", after) or "--replace-hand-made" in after, "the edit with --replace-hand-made is not after the yes"


def test_the_permission_prompt_is_not_claimed_in_every_permission_mode():
    """The independent check, finding 2: in the bypass and auto permission modes Claude Code's prompt does not ask. The README and
    the skill say 'where your permission settings ask' (the skill: the owner's), or nothing."""
    readme = " ".join((PLUGIN / "README.md").read_text().split())
    skill = " ".join(SKILL_LINK.read_text().split())
    for label, text in (("README.md", readme), ("the skill", skill)):
        for m in re.finditer(r"permission prompt", text):
            clause = text[max(0, m.start() - 90): m.end() + 40]
            assert re.search(r"where (?:your|the owner's) permission settings ask", clause, re.I), \
                f"{label} claims the prompt without a condition: {clause}"
    assert "permission prompt" in readme or "permission prompt" in skill


def test_the_readme_says_remove_puts_the_command_back_and_the_row_comes_back_by_the_ten_minute_rule():
    """The independent check, finding 2: the script never touches the switches. By hand, `remove` gives the old command back;
    the row comes back within ten minutes, or the owner turns it on in /config (the skill's own remove step turns it on)."""
    text = " ".join((PLUGIN / "README.md").read_text().split())
    assert "turns the row back on" not in text, "README.md says the script turns the row on"
    assert re.search(r"puts your old command back exactly; the row comes back within ten minutes \(or turn it on in `/config`\)", text), \
        "README.md does not say that the row comes back within ten minutes, or by /config"


def test_no_gendered_pronoun_in_the_skill_the_readme_sections_on_the_link_and_the_files_that_describe_them():
    """The plugin is public and its skills run for any owner: 'the owner', 'they', 'their', or a sentence that needs no pronoun.
    Asked for by the independent check twice on 15 Nov (a template, a skill sentence), and again for this skill."""
    word = re.compile(r"\b(?:he|his|him|himself|she|her|hers|herself)\b", re.I)
    readme = (PLUGIN / "README.md").read_text()
    sections = readme[readme.index("### The board's link is in the session"): readme.index("### Under Orca")]
    texts = {
        "skills/board-link/SKILL.md": SKILL_LINK.read_text(),
        "README.md, the sections on the link": sections,
        ".claude-plugin/plugin.json": MANIFEST.read_text(),
        "hooks/board-link.tsx": MOD.read_text(),
        "status-board/board_link.py": HELPER.read_text(),
        "tests/board-link.test.tsx": (PLUGIN / "tests" / "board-link.test.tsx").read_text(),
    }
    for label, text in texts.items():
        found = [m.group(0) for m in word.finditer(text)]
        assert not found, f"{label} has a gendered pronoun: {found[:6]}"


def test_no_machine_path_in_shipped_files():
    for path in (SKILL_LINK, MOD, HELPER, MANIFEST, PLUGIN / "README.md", PLUGIN / "skills" / "project-bootstrap" / "SKILL.md"):
        text = path.read_text()
        assert not re.search(r"/opt/homebrew|/Users/[A-Za-z]|/home/[A-Za-z]", text), f"{path.name} names a machine path"


def test_the_bootstrap_skill_asks_where_the_boards_link_shows_when_never_asked():
    text = (PLUGIN / "skills" / "project-bootstrap" / "SKILL.md").read_text()
    section = text[text.index("## Ask the owner first"): text.index("## Copy from `templates/`")]
    assert "where the board's link shows" in section and "/project-init:board-link" in section, section
    assert re.search(r"never (?:been )?asked", section), "the question is asked only of an owner who was never asked"
    assert section.count("/project-init:board-link") == 1, "one question, not a second flow"


def test_the_status_board_skill_says_where_the_switches_are_in_one_sentence():
    text = (PLUGIN / "skills" / "status-board" / "SKILL.md").read_text()
    sentence = ("Where the board's link shows (above the prompt, in the status line, or both) is the owner's choice: the plugin's two "
                "switches, `/project-init:board-link`; you do not change them yourself.")
    assert text.count(sentence) == 1, f"the sentence is there {text.count(sentence)} times"


def test_claude_validates_the_plugin_with_the_mod():
    claude = shutil.which("claude")
    if not claude:
        raise Skip("claude is not on PATH")
    done = subprocess.run([claude, "plugin", "validate", str(PLUGIN)], capture_output=True, text=True, timeout=90,
                          env={"HOME": str(FAKE_HOME), "PATH": os.environ.get("PATH", "")}, cwd=str(TMP))
    out = done.stdout + done.stderr
    assert "board-link.tsx hooks:" in out, out[-600:]
    assert "session.start" in out and "AbovePrompt" in out, out[-600:]
    assert "Validation failed" not in out and done.returncode == 0, out[-600:]


def main() -> int:
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
                reason = " ".join(str(exc).split())[:300] or type(exc).__name__
                print(f"FAIL  {name}: {type(exc).__name__}: {reason}")
    finally:
        for proc in PROCESSES:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        left = sum(1 for proc in PROCESSES if proc.poll() is None)
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed - skipped} passed, {failed} failed, {skipped} skipped, processes left running: {left}, plugin under test: {PLUGIN}")
    return 1 if failed or left else 0


if __name__ == "__main__":
    sys.exit(main())
