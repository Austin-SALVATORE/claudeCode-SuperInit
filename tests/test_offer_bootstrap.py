#!/usr/bin/env python3
"""The offer hook offers the Orca-only bootstrap, inside Orca, and nowhere else (15 Nov 2026).

    python3 tests/test_offer_bootstrap.py --plugin <plugin dir>

What is tested: hooks/offer-bootstrap.py, run as a real process the way Claude Code
runs it at SessionStart: a JSON payload on stdin, the output read from stdout. One
test for each row of the table of sessions in BOOTSTRAP-ORCA-PLAN.md section 3 (a
coordinator in a project with no scaffold, a worktree of a bootstrapped one, a
worker, an agent, an old set, a new set, a folder that is not a project, outside
Orca, resume/compact/fork), the three named in the plan (the update note, the
opt-out, any error) and the rules every hook of this plugin keeps: it writes
nothing, it asks no network, it is quick even when Orca hangs.

Nothing here touches the owner's home, a real repository or the real Orca. Every
run has a temporary HOME and STATUS_BOARD_HOME, scratch git repositories and
`git worktree add`, and a PATH that holds only a stand-in `orca` (which logs its
arguments and refuses, or hangs, or lists one live worker) and `ps`. No test
starts a server or binds a port.

Run it against the code before the change and each test fails for the reason it
names (the offer still says "no .claude/ team structure", still reads team-roles.md,
still offers outside Orca and in a folder that is not a project); the tests that
guard what must not change (a worker, an agent, a non-startup source, the opt-out,
the quiet failure) pass on both.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="the plugin directory under test")
ap.add_argument("--only", default="", help="run only the tests whose name contains this")
ARGS = ap.parse_args()
PLUGIN = Path(ARGS.plugin).resolve()
HOOKS_DIR = PLUGIN / "hooks"
HOOK = HOOKS_DIR / "offer-bootstrap.py"
sys.dont_write_bytecode = True

TMP = Path(tempfile.mkdtemp(prefix="offer-bootstrap-")).resolve()
HOME = TMP / "home"
SB_HOME = TMP / "board-home"
PROJECTS = TMP / "projects"
FAKE_BIN = TMP / "bin"
FIXTURES = TMP / "fixtures"
HANDLE = "term_00000000-1111-2222-3333-444444444444"  # invented: no real Orca terminal has it

# What Orca's preamble to a worker looks like (orca_session.DISPATCH matches it).
DISPATCH = ("You are a dispatched worker.\nYour coordinator's terminal handle is: "
            "term_00000000-1111-2222-3333-444444444444\nYour task ID is: task_0123456789ab\n")

FAKE_ORCA = """#!{python}
import json, os, sys, time
where, mode, args = os.environ["FAKE_ORCA_DIR"], os.environ.get("FAKE_ORCA_MODE", "refuse"), sys.argv[1:]
with open(os.path.join(where, "calls.log"), "a") as log:
    log.write(" ".join(args) + "\\n")
if mode == "hang":
    time.sleep(30)
if mode == "live" and args[:2] == ["orchestration", "worker-list"]:
    sys.stdout.write(open(os.path.join(where, "workers.json")).read())
    sys.exit(0)
sys.stdout.write('{{"ok": false, "error": {{"code": "refused"}}}}')
sys.exit(2)
"""


def setup() -> None:
    for d in (HOME, SB_HOME, PROJECTS, FAKE_BIN, FIXTURES):
        d.mkdir(parents=True)
    exe = FAKE_BIN / "orca"
    exe.write_text(FAKE_ORCA.format(python=sys.executable))
    exe.chmod(0o755)
    (FAKE_BIN / "ps").symlink_to(shutil.which("ps") or "/bin/ps")
    claude = FAKE_BIN / "claude"  # a stand-in that logs its call: the hook must never ask for the plugin list
    claude.write_text(f'#!{sys.executable}\nimport os, sys\nopen(os.path.join(os.environ["FAKE_ORCA_DIR"], "claude.log"), "a").write(" ".join(sys.argv[1:]) + "\\n")\n')
    claude.chmod(0o755)
    live = {"verdict": "live", "observedAt": int(time.time() * 1000)}
    row = {"dispatchId": "ctx_0123456789ab", "taskId": "task_0123456789ab", "runId": "run_0123456789ab",
           "workerState": "ready", "agentTerminalHandle": HANDLE,
           "projection": {"liveness": live, "stage": {"activity": "working"}, "workspace": {"id": "repo::/Users/x/shop-app"}}}
    (FIXTURES / "workers.json").write_text(json.dumps({"ok": True, "result": {"workers": [row], "scope": {"source": "all"}}}))


# ── scratch repositories ─────────────────────────────────────────────
def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


SCAFFOLD = {"new": ".claude/rules/operating-model.md", "old": ".claude/rules/team-roles.md"}
_counter = [0]


def project(name: str, scaffold: str = "none", committed: bool = True, base: Path | None = None) -> Path:
    """A scratch git repository with one commit. scaffold: none, new (operating-model.md), old (team-roles.md) or both."""
    _counter[0] += 1
    repo = (base or PROJECTS) / f"{name}-{_counter[0]}"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "scratch")
    kinds = ("new", "old") if scaffold == "both" else (scaffold,) if scaffold in SCAFFOLD else ()
    for kind in kinds:
        path = repo / SCAFFOLD[kind]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# scratch\n")
    if kinds and committed:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "scaffold")
    return repo


def worktree(main: Path, name: str = "wt") -> Path:
    """A git worktree of `main` on a branch of its own, the way Orca makes a child worktree."""
    path = main.parent / f"{main.name}-{name}"
    git(main, "worktree", "add", "-q", "-b", f"{name}-{main.name}", str(path))
    return path


# ── running the hook ─────────────────────────────────────────────────
class Result:
    def __init__(self, proc: subprocess.CompletedProcess, seconds: float):
        self.code, self.out, self.err, self.seconds = proc.returncode, proc.stdout, proc.stderr, seconds

    @property
    def offered(self) -> bool:
        return "<bootstrap-offer>" in self.out

    @property
    def noted(self) -> bool:
        return "<bootstrap-update>" in self.out

    @property
    def silent(self) -> bool:
        return self.code == 0 and not self.out and not self.err


def run_hook(cwd, *, source: str = "startup", orca: bool = True, env: dict | None = None, payload: dict | None = None,
             stdin: str | None = None, ancestor_flags: tuple = (), hooks_dir: Path | None = None,
             orca_mode: str = "refuse", transcript: Path | None = None, timeout: int = 30) -> Result:
    body = {"hook_event_name": "SessionStart", "session_id": "session-1", "source": source, "cwd": str(cwd)}
    if transcript is not None:
        body["transcript_path"] = str(transcript)
    body.update(payload or {})
    hook = (hooks_dir or HOOKS_DIR) / "offer-bootstrap.py"
    command = [sys.executable, "-B", str(hook)]
    if ancestor_flags:  # a shell whose own command line carries the flags, as a split-pane teammate's process does
        # The shell must not exec the hook in its place (sh does, for a last simple command): it has to stay its parent.
        command = ["/bin/sh", "-c", '"$0" -B "$1"; status=$?; exit $status', sys.executable, str(hook), *ancestor_flags]
    # The PATH is the stand-in orca and ps, nothing else: the hook must not need git, nor reach the real Orca.
    environment = {"HOME": str(HOME), "PATH": str(FAKE_BIN), "STATUS_BOARD_HOME": str(SB_HOME),
                   "PYTHONDONTWRITEBYTECODE": "1", "FAKE_ORCA_DIR": str(FIXTURES), "FAKE_ORCA_MODE": orca_mode}
    if orca:
        environment["ORCA_TERMINAL_HANDLE"] = HANDLE
    environment.update(env or {})
    started = time.time()
    proc = subprocess.run(command, input=json.dumps(body) if stdin is None else stdin, capture_output=True, text=True,
                          env=environment, timeout=timeout, cwd=str(TMP))
    return Result(proc, time.time() - started)


def worker_transcript() -> Path:
    path = TMP / f"transcript-{time.time_ns()}.jsonl"
    lines = [{"type": "attachment", "note": "session start"},
             {"type": "user", "message": {"role": "user", "content": DISPATCH}}]
    path.write_text("\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n")
    return path


def said(result: Result) -> str:
    return f"exit {result.code}, stdout {result.out[:120]!r}, stderr {result.err[:120]!r}"


# ── the table of sessions: one test per row ──────────────────────────
def test_row1_a_coordinator_in_orca_in_a_project_with_no_scaffold_is_offered():
    repo = project("shop-app")
    for source in ("startup", "clear"):
        result = run_hook(repo, source=source)
        assert result.code == 0 and result.offered and not result.noted, f"{source}: {said(result)}"
        assert "operating-model.md" in result.out, f"the offer must name the file that ends it: {result.out[:200]!r}"
        assert "team-roles.md" not in result.out and "team structure" not in result.out, result.out[:300]
        assert f"({repo})" in result.out, "the offer names the project's root"
    sub = repo / "src" / "app"
    sub.mkdir(parents=True)
    from_sub = run_hook(sub)
    assert from_sub.offered and f"({repo})" in from_sub.out, f"from a sub-folder, the root is named: {from_sub.out[:160]!r}"
    assert run_hook(project("notes", base=home_projects())).offered, "a project inside the home folder is a project"


def home_projects() -> Path:
    path = HOME / "code"
    path.mkdir(exist_ok=True)
    return path


def test_row2_a_worktree_of_a_bootstrapped_project_is_silent_even_before_the_scaffold_is_committed():
    committed = project("shop-app", "new")
    child = worktree(committed)
    assert (child / SCAFFOLD["new"]).is_file(), "the worktree of a committed scaffold has it already"
    uncommitted = project("notes", "new", committed=False)
    child2 = worktree(uncommitted)
    assert not (child2 / SCAFFOLD["new"]).exists(), "a scaffold that is only in the main checkout is not in its worktree"
    sub = child2 / "src"
    sub.mkdir()
    for where in (child, child2, sub):
        result = run_hook(where)
        assert result.silent, f"{where.name}: {said(result)}"
    assert run_hook(worktree(project("blog")), orca=True).offered, "a worktree of a project with no scaffold anywhere is a project with none"


def test_row3_an_orca_worker_is_silent():
    repo = project("shop-app")
    by_transcript = run_hook(repo, transcript=worker_transcript())
    assert by_transcript.silent, f"worker known by its first prompt: {said(by_transcript)}"
    by_orca = run_hook(repo, orca_mode="live")
    assert by_orca.silent, f"worker known by Orca's own record of a live dispatch: {said(by_orca)}"
    assert "worker-list" in (FIXTURES / "calls.log").read_text(), "the stand-in Orca was asked, and not the real one"
    assert run_hook(repo, orca_mode="refuse").offered, "the same session with no dispatch listed is a coordinator"


def test_row4_a_pane_teammate_or_an_agent_session_is_silent():
    repo = project("shop-app")
    for extra in ({"agent_type": "reviewer"}, {"agent_id": "a1"}, {"teammate_name": "dev"}, {"team_name": "t"}):
        result = run_hook(repo, payload=extra)
        assert result.silent, f"{extra}: {said(result)}"
    for flags in (("--agent-id=a1",), ("--parent-session-id", "s0")):
        result = run_hook(repo, ancestor_flags=flags)
        assert result.silent, f"an ancestor launched with {flags}: {said(result)}"
    assert run_hook(repo, ancestor_flags=("--model", "x")).offered, "an ancestor with other flags is the owner's own session"


def test_row5_a_project_with_the_old_set_only_gets_the_update_note_and_not_the_offer():
    repo = project("shop-app", "old")
    result = run_hook(repo)
    assert result.code == 0 and result.noted and not result.offered, said(result)
    assert "/project-bootstrap update" in result.out and "team-roles.md" in result.out, result.out[:300]
    assert f"({repo})" in result.out, "the note names the project's root"
    child = worktree(repo)
    assert run_hook(child).noted, "a worktree of a project with the old set is told too (its main checkout is found)"


def test_row6_a_project_with_the_new_set_is_silent():
    for scaffold in ("new", "both"):  # `both`: a project in the middle of the move, with the old file still beside the new one
        result = run_hook(project("shop-app", scaffold))
        assert result.silent, f"{scaffold}: {said(result)}"
    assert run_hook(project("notes", "new", committed=False)).silent, "an uncommitted scaffold counts as there"


def test_row7_a_folder_that_is_not_a_project_is_silent():
    plain = TMP / "plain-folder"
    plain.mkdir()
    dotfiles = HOME / "dotsub"
    dotfiles.mkdir()
    (HOME / ".git").mkdir()  # a dotfiles repository makes $HOME a git top-level: that is not a project
    claude = HOME / ".claude" / "projects" / "x"
    claude.mkdir(parents=True)
    try:
        for where in (plain, HOME, dotfiles, Path("/"), HOME / ".claude", claude):
            result = run_hook(where)
            assert result.silent, f"{where}: {said(result)}"
    finally:
        shutil.rmtree(HOME / ".git")


def test_row8_outside_orca_is_silent_whatever_the_project():
    for scaffold in ("none", "old"):
        repo = project("shop-app", scaffold)
        result = run_hook(repo, orca=False)
        assert result.silent, f"{scaffold}: {said(result)}"
    assert run_hook(project("notes"), orca=False, env={"TERM_PROGRAM": "Orca"}).silent, "only the terminal handle says this is Orca"
    assert run_hook(project("blog"), orca=True).offered, "and with the handle set the same project is offered"


def test_row9_resume_compact_and_fork_are_silent():
    repo = project("shop-app")
    for source in ("resume", "compact", "fork", "", "anything-else"):
        result = run_hook(repo, source=source)
        assert result.silent, f"{source!r}: {said(result)}"
    no_source = run_hook(repo, stdin=json.dumps({"cwd": str(repo)}))
    assert no_source.silent, f"a payload with no source: {said(no_source)}"
    assert run_hook(repo, source="startup").offered and run_hook(repo, source="clear").offered


# ── the three the plan names ─────────────────────────────────────────
def test_update_note_for_old_set_only():
    old, both, none = project("shop-app", "old"), project("notes", "both"), project("blog")
    note = run_hook(old)
    assert note.noted and not note.offered, said(note)
    assert "Do not edit those files yourself" in note.out and "no-bootstrap-offer" in note.out, note.out
    assert note.out.count("<bootstrap-update>") == 1 and note.out.count("</bootstrap-update>") == 1
    # The old set loses the tree lock's deny on its own writing agents (review of 15 Nov 2026, finding 4): say so.
    assert "tree lock no longer runs" in note.out and "one writing agent at a time" in note.out, note.out
    assert run_hook(both).silent, "with operating-model.md there is nothing to tell"
    assert run_hook(none).offered and not run_hook(none).noted, "no set at all is the offer"
    assert run_hook(old, source="resume").silent and run_hook(old, source="compact").silent, "the note is for startup and clear"
    assert run_hook(old, source="clear").noted
    assert run_hook(old, transcript=worker_transcript()).silent, "never in a worker"


def test_opt_out_silences_offer_and_update_note():
    for scaffold in ("none", "old"):
        repo = project("shop-app", scaffold)
        assert run_hook(repo, env={"PROJECT_INIT_NO_OFFER": "1"}).silent, f"{scaffold}: the variable"
        zero = run_hook(repo, env={"PROJECT_INIT_NO_OFFER": "0"})
        assert zero.code == 0 and (zero.offered or zero.noted), f"{scaffold}: 0 does not opt out: {said(zero)}"
        (repo / ".claude").mkdir(exist_ok=True)
        (repo / ".claude" / "no-bootstrap-offer").write_text("")
        assert run_hook(repo).silent, f"{scaffold}: the empty file at the project root"
        sub = repo / "src"
        sub.mkdir()
        assert run_hook(sub).silent, f"{scaffold}: the file is found from a sub-folder"
        assert run_hook(worktree(repo)).silent, f"{scaffold}: and from a worktree, by its main checkout"
    own = project("notes")
    sub = own / "docs"
    (sub / ".claude").mkdir(parents=True)
    (sub / ".claude" / "no-bootstrap-offer").write_text("")
    assert run_hook(sub).silent, "the file at the cwd itself"
    assert run_hook(own).offered, "and it only silences what it sits in"


def test_any_error_prints_nothing_exit_0():
    repo = project("shop-app")
    for label, stdin in (("empty stdin", ""), ("not JSON", "not json {"), ("a list", "[]"), ("null", "null"),
                         ("no cwd", json.dumps({"source": "startup"})), ("cwd is a number", json.dumps({"source": "startup", "cwd": 7})),
                         ("cwd that does not exist", json.dumps({"source": "startup", "cwd": str(TMP / "nowhere")})),
                         ("cwd is a file", json.dumps({"source": "startup", "cwd": str(HOOK)})),
                         ("source is a list", json.dumps({"source": ["startup"], "cwd": str(repo)}))):
        result = run_hook(repo, stdin=stdin)
        assert result.silent, f"{label}: {said(result)}"
    broken = TMP / "broken-hooks"  # a plugin whose orca_session.py cannot be imported: the hook must not fail the session
    shutil.copytree(HOOKS_DIR, broken, ignore=shutil.ignore_patterns("__pycache__"))
    (broken / "orca_session.py").write_text("def broken(:\n")
    result = run_hook(repo, hooks_dir=broken)
    assert result.code == 0 and not result.err and "Traceback" not in result.out, said(result)
    gone = TMP / "hooks-without-session"
    shutil.copytree(HOOKS_DIR, gone, ignore=shutil.ignore_patterns("__pycache__"))
    (gone / "orca_session.py").unlink()
    result = run_hook(repo, hooks_dir=gone)  # no way to tell a worker or a teammate: noise at worst, never an error
    assert result.code == 0 and not result.err and "Traceback" not in result.out, said(result)
    wt = project("notes")
    (wt / ".git").rename(wt / ".git-real")
    (wt / ".git").write_text("gitdir: /does/not/exist/worktrees/x\n")  # a worktree whose main checkout is gone
    assert run_hook(wt).code == 0 and not run_hook(wt).err, "a `.git` file that points nowhere is not an error"
    (wt / ".git").write_bytes(b"\xff\xfe garbage \x00")
    assert run_hook(wt).code == 0 and not run_hook(wt).err, "a `.git` file that is not text is not an error"
    locked = project("blog")
    (locked / ".claude").mkdir()
    (locked / ".claude").chmod(0o000)
    try:
        result = run_hook(locked)
        assert result.code == 0 and not result.err and (not result.out or result.out.startswith("<bootstrap-")), said(result)
    finally:
        (locked / ".claude").chmod(0o755)


# ── what the main checkout does, and what the hook must never do ──────
def test_the_main_checkout_is_found_without_git():
    repo = project("shop-app", "new", committed=False)
    child = worktree(repo)
    git_file = (child / ".git").read_text()
    assert git_file.startswith("gitdir:") and "/worktrees/" in git_file, f"not a worktree's .git file: {git_file!r}"
    assert shutil.which("git", path=str(FAKE_BIN)) is None, "the hook's PATH must hold no git for this test to mean anything"
    result = run_hook(child)
    assert result.silent, f"the worktree of a bootstrapped project, with no git on PATH: {said(result)}"


def test_the_hook_writes_nothing():
    repo = project("shop-app")
    child = worktree(repo)
    def snapshot() -> dict:
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for root in (TMP, PLUGIN / "hooks", PLUGIN / "status-board")
                for p in root.rglob("*") if p.is_file() and "calls.log" not in p.name and ".git/" not in str(p)}
    before = snapshot()
    for where in (repo, child, HOME, repo / ".git"):
        run_hook(where)
    run_hook(repo, orca_mode="live")
    after = snapshot()
    assert before == after, f"changed: {sorted(set(after.items()) ^ set(before.items()))[:3]}"
    assert not list((PLUGIN / "hooks").rglob("__pycache__")) and not list((PLUGIN / "status-board").rglob("__pycache__"))
    tree = ast.parse(HOOK.read_text())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"urllib", "http", "socket", "requests", "shutil", "tempfile"}, f"imports: {sorted(imported)}"
    # The bootstrap offer writes nothing. The board link's block (functions named `board_link*`, release 2.3.0) may write
    # two files in the plugin's data folder and nothing else: see test_the_board_link_block_writes_only_in_the_data_folder.
    rest = ast.Module(body=[n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name.startswith("board_link"))], type_ignores=[])
    writers = {n.attr for n in ast.walk(rest) if isinstance(n, ast.Attribute)} & {
        "write_text", "write_bytes", "mkdir", "touch", "unlink", "rename", "replace", "open", "write_text", "chmod"}
    opens = [n for n in ast.walk(rest) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"]
    assert not writers and not opens, f"offer-bootstrap.py writes or opens: {sorted(writers)} {len(opens)}"


def test_a_hung_orca_costs_the_hook_about_a_second():
    repo = project("shop-app")
    result = run_hook(repo, orca_mode="hang")
    assert result.offered, f"a hung Orca must not decide the offer either way: {said(result)}"
    assert result.seconds < 3.5, f"the hook took {result.seconds:.1f} s"
    quick = run_hook(repo)
    assert quick.seconds < 2.5, f"a run with a refusing Orca took {quick.seconds:.1f} s"


def test_the_wording_says_what_to_ask_and_how_to_stop():
    out = run_hook(project("shop-app")).out
    for phrase in ("no Orca operating model", ".claude/rules/operating-model.md", "main checkout", "/project-bootstrap",
                   "Ask what the project is first", "no file until", "do not raise it again this session",
                   ".claude/no-bootstrap-offer", "PROJECT_INIT_NO_OFFER=1"):
        assert phrase in out, f"missing {phrase!r} in:\n{out}"
    assert out.startswith("<bootstrap-offer>\n") and out.rstrip().endswith("</bootstrap-offer>"), out


def hooks_readme_text(event: str, number: int) -> str:
    """What describes a hook group: the last paragraph of its section in hooks/README.md. It was the group's `description` key in
    hooks.json until Claude Code reported that key as unknown (15 Nov 2026); the documentation names no such key."""
    text = (HOOKS_DIR / "README.md").read_text()
    for block in re.split(r"(?m)^## ", text)[1:]:
        heading, _, body = block.partition("\n")
        if heading.split()[0] == f"{event}[{number}]":
            return [p for p in body.strip("\n").split("\n\n") if p.strip()][-1]
    raise AssertionError(f"hooks/README.md has no section for {event}[{number}]")


def test_hooks_json_describes_the_new_offer():
    config = json.loads((HOOKS_DIR / "hooks.json").read_text())
    entries = [(n, group) for n, group in enumerate(config["hooks"]["SessionStart"]) if "offer-bootstrap.py" in json.dumps(group)]
    assert len(entries) == 1, "the offer hook is registered once on SessionStart"
    number, entry = entries[0]
    description = hooks_readme_text("SessionStart", number)
    assert "operating-model.md" in description, description[:200]
    assert "Orca" in description and "team structure" not in description, description[:300]
    assert "CLAUDE_CODE_SESSION_ATTENDED" in description and "sdk" in description, "the runs nobody attends are named: " + description[:300]
    assert entry["hooks"][0]["timeout"] <= 10, "a hook of every session start stays quick"


# ── the review of 15 Nov 2026 (REVIEW-2.2.0.md) ───────────────────────
def test_a_run_that_is_not_attended_is_silent():
    """Finding 1: `claude -p` sets CLAUDE_CODE_SESSION_ATTENDED=0, and nobody is there to answer an offer."""
    for scaffold in ("none", "old"):
        repo = project("shop-app", scaffold)
        for source in ("startup", "clear"):
            result = run_hook(repo, source=source, env={"CLAUDE_CODE_SESSION_ATTENDED": "0"})
            assert result.silent, f"{scaffold}/{source}: {said(result)}"
        for value in ("1", "", "true"):  # only "0" says nobody is there; an interactive session says "1"
            result = run_hook(repo, env={"CLAUDE_CODE_SESSION_ATTENDED": value})
            assert result.offered or result.noted, f"{scaffold}, attended={value!r}: {said(result)}"
    assert run_hook(project("notes"), orca_mode="live", env={"CLAUDE_CODE_SESSION_ATTENDED": "0"}).silent


def test_an_sdk_entrypoint_is_silent():
    """Finding 1: `claude -p` and the SDK have CLAUDE_CODE_ENTRYPOINT=sdk-cli (or sdk-ts, sdk-py...)."""
    for scaffold in ("none", "old"):
        repo = project("shop-app", scaffold)
        for entry in ("sdk-cli", "sdk-ts", "sdk-py", "sdk"):
            result = run_hook(repo, env={"CLAUDE_CODE_ENTRYPOINT": entry})
            assert result.silent, f"{scaffold}, entrypoint {entry!r}: {said(result)}"
        for entry in ("cli", "claude-desktop", "", "not-sdk"):  # whatever does not start with sdk is a person's session
            result = run_hook(repo, env={"CLAUDE_CODE_ENTRYPOINT": entry})
            assert result.offered or result.noted, f"{scaffold}, entrypoint {entry!r}: {said(result)}"
    repo = project("notes")
    assert run_hook(repo, env={"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}).silent, "either variable is enough"
    assert run_hook(repo, env={"CLAUDE_CODE_SESSION_ATTENDED": "0", "CLAUDE_CODE_ENTRYPOINT": "cli"}).silent, "either variable is enough"


def test_with_both_variables_missing_the_offer_is_made_as_today():
    """Finding 1: an older Claude Code sets neither variable; the hook then behaves as it did."""
    repo, old = project("shop-app"), project("notes", "old")
    result = run_hook(repo)  # run_hook's environment holds neither variable
    assert result.offered, said(result)
    assert run_hook(old).noted, "and the update note too"
    both_interactive = run_hook(repo, env={"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert both_interactive.offered, f"an interactive session (1, cli): {said(both_interactive)}"


def test_a_commondir_that_is_not_a_regular_file_cannot_hang_the_hook():
    """Finding 3: a FIFO read blocks until Claude Code ends the hook at its 10 s; commondir is read only if it is a small regular file."""
    main = project("shop-app")
    gitdir = main / ".git" / "worktrees" / "fifo"
    gitdir.mkdir(parents=True)
    os.mkfifo(gitdir / "commondir")
    fake = PROJECTS / "fifo-worktree"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {gitdir}\n")
    result = run_hook(fake, timeout=8)  # a hook that blocks raises TimeoutExpired here, and the test fails
    assert result.offered and result.seconds < 3, f"{result.seconds:.1f} s: {said(result)}"
    # And a commondir of more than a few KB is not read: the worktree is then its own project, not one with a main checkout.
    bootstrapped = project("notes", "new", committed=False)
    child = worktree(bootstrapped)
    assert run_hook(child).silent, "a real worktree finds its main checkout"
    child_gitdir = Path((child / ".git").read_text().split(":", 1)[1].strip())
    (child_gitdir / "commondir").write_text("../..\n" + " " * 8192)
    assert run_hook(child).offered, "a commondir of 8 KB is not trusted"


def test_a_worker_or_a_pane_teammate_in_an_old_set_project_gets_no_update_note():
    """Finding 7: the note comes after the agent gate; nothing else pinned that order (rows 3 and 4 use a project with no scaffold)."""
    old = project("shop-app", "old")
    assert run_hook(old).noted, "the owner's own session is told"
    cases = {
        "a worker, by its first prompt": run_hook(old, transcript=worker_transcript()),
        "a worker, by a live dispatch in Orca's record": run_hook(old, orca_mode="live"),
        "an agent_type in the payload": run_hook(old, payload={"agent_type": "reviewer"}),
        "an agent_id in the payload": run_hook(old, payload={"agent_id": "a1"}),
        "a teammate_name in the payload": run_hook(old, payload={"teammate_name": "dev"}),
        "an ancestor launched with --agent-id": run_hook(old, ancestor_flags=("--agent-id=a1",)),
        "an ancestor launched with --parent-session-id": run_hook(old, ancestor_flags=("--parent-session-id", "s0")),
    }
    for label, result in cases.items():
        assert result.silent, f"{label}: {said(result)}"
    assert run_hook(worktree(old), transcript=worker_transcript()).silent, "a worker in a worktree of an old-set project"


def test_a_submodule_is_its_own_project():
    """Finding 7, a guard of what happens: a submodule's `.git` file has no commondir, so it is judged on its own files."""
    bare_lib, lib_with_model = project("lib"), project("lib-bootstrapped", "new")
    superproject = project("shop-app", "new")
    for lib, path in ((bare_lib, "libs/plain"), (lib_with_model, "libs/modeled")):
        git(superproject, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), path)
    plain = run_hook(superproject / "libs" / "plain")
    assert plain.offered, f"the superproject's scaffold does not cover its submodule: {said(plain)}"
    assert run_hook(superproject / "libs" / "modeled").silent, "a submodule with its own operating model is silent"


def test_a_worktree_of_a_bare_repository_is_its_own_project():
    """Finding 7, a guard of what happens: the common dir of a bare repository is not named `.git`, so there is no main checkout."""
    source = project("shop-app")
    bare = PROJECTS / "shop-app.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(bare)], check=True, capture_output=True)
    child = PROJECTS / "bare-worktree"
    git(bare, "worktree", "add", "-q", "-b", "wt-branch", str(child))
    assert (child / ".git").is_file()
    nowhere = run_hook(child)
    assert nowhere.offered, f"scaffold nowhere: {said(nowhere)}"
    (child / ".claude" / "rules").mkdir(parents=True)
    (child / SCAFFOLD["new"]).write_text("# scratch\n")
    git(child, "add", "-A")
    git(child, "commit", "-q", "-m", "scaffold")
    assert run_hook(child).silent, "scaffold on the worktree's own branch"


def test_the_docs_say_what_the_hook_does():
    """Findings 1, 5 and 6: the README, the skill, the template and the hook's docstring say what the built hook does."""
    flat = " ".join((PLUGIN / "README.md").read_text().split())
    assert "CLAUDE_CODE_SESSION_ATTENDED" in flat and "sdk" in flat, "the README's list of silent cases names the runs nobody attends"
    assert "(checked at the working directory, at its git top-level and in its main checkout)" in flat, "the opt-out file is also found in the main checkout"
    assert "told once per session, at startup or `/clear`" in flat, "the update note is told at every start, so once per session"
    templates = PLUGIN / "skills" / "project-bootstrap" / "templates"
    read_at_start = [templates / "CLAUDE.md", *(templates / "rules" / n for n in ("operating-model.md", "verification.md", "release-choreography.md"))]
    total = sum(len(p.read_text().splitlines()) for p in read_at_start)
    skill = (PLUGIN / "skills" / "project-bootstrap" / "SKILL.md").read_text()
    assert 295 <= total <= 305, f"the four files a session reads at its start have {total} lines"
    assert "about 300 lines a session reads at its start" in " ".join(skill.split()) and "about 290" not in skill, "the skill's count"
    model = (templates / "rules" / "operating-model.md").read_text()
    assert not re.search(r"\b(he|him|his|himself)\b", model, re.IGNORECASE), "a template copied into every project names its owner neutrally"
    assert "every choice that is the owner's" in model
    doc = " ".join((ast.get_docstring(ast.parse(HOOK.read_text())) or "").split())
    assert "two calls" in doc and "2 s at most" in doc and "1 s" in doc, "the docstring names both calls that can wait"


# ── release 2.3.0: the second block, the board link's one-time lines (OPTION-PLAN.md section 2 and 3) ───────────────
PLUGIN_KEY = "project-init@claudecode-superinit"
MARKER = "statusline-board-link.py"
_fresh = [0]


def fresh_data() -> Path:
    """A plugin data folder that does not exist yet (the hook is given its path in CLAUDE_PLUGIN_DATA and creates it)."""
    _fresh[0] += 1
    return TMP / f"plugin-data-{_fresh[0]}"


def with_board(repo: Path) -> Path:
    """A board for this project: its folder under the status-board home, naming the project (repo.json)."""
    board = SB_HOME / "boards" / repo.name.lower()
    board.mkdir(parents=True, exist_ok=True)
    (board / "repo.json").write_text(json.dumps({"repo": str(repo.resolve())}))
    (board / "narrative.json").write_text(json.dumps({"headline": "x"}))
    return board


def link_env(data: Path | None, config: Path | None = None, **extra: str) -> dict:
    env = {"CLAUDE_CONFIG_DIR": str(config or TMP / "config-none")}
    if data is not None:
        env["CLAUDE_PLUGIN_DATA"] = str(data)
    env.update(extra)
    return env


def settings_for(config: Path, command: str | None, status_line_on: bool = True) -> None:
    config.mkdir(parents=True, exist_ok=True)
    body: dict = {"pluginConfigs": {PLUGIN_KEY: {"options": {"board_link_status_line": status_line_on}}}}
    if command is not None:
        body["statusLine"] = {"type": "command", "command": command}
    (config / "settings.json").write_text(json.dumps(body))


def wired_record(data: Path) -> None:
    """What `statusline_setup.py record` and `add` leave: the owner's answer, and the old command kept (the status line was wired)."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "board-link.json").write_text(json.dumps({"v": 1, "answer": "status-line"}))
    (data / "statusline-before.json").write_text(json.dumps({"v": 1, "command": "echo before", "after_command": "x " + MARKER}))


def board_project(scaffold: str = "new") -> Path:
    """A project that has an operating model (so the bootstrap's block says nothing) and a board."""
    repo = project("shop-app", scaffold)
    with_board(repo)
    return repo


def test_board_link_offer_is_made_once_to_the_owners_own_session_inside_or_outside_orca():
    for orca in (False, True):
        repo, data = board_project(), fresh_data()
        result = run_hook(repo, orca=orca, env=link_env(data))
        assert result.code == 0 and "<board-link-offer>" in result.out and result.out.rstrip().endswith("</board-link-offer>"), said(result)
        for phrase in ("/project-init:board-link", "above the prompt", "status line", "do not raise it again", "the row above the prompt stays"):
            assert phrase in result.out, f"missing {phrase!r} in:\n{result.out}"
        assert result.out.count("<board-link-offer>") == 1 and not result.offered, "one block, and no bootstrap offer in a project that has its model"
        assert (data / "board-link.offered").is_file(), "the marker that makes it once per user"
        again = run_hook(repo, orca=orca, env=link_env(data))
        assert again.silent, f"orca={orca}: the second session says nothing: {said(again)}"
        other_project = board_project()
        assert run_hook(other_project, orca=orca, env=link_env(data)).silent, "once per user, not once per project"


def test_two_sessions_starting_together_give_the_board_link_offer_once():
    repo, data = board_project(), fresh_data()
    go = TMP / f"go-{time.time_ns()}"
    wait = ("import os, subprocess, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.005)\n"
            "sys.exit(subprocess.run(sys.argv[2:], stdin=sys.stdin).returncode)\n")
    environment = {"HOME": str(HOME), "PATH": str(FAKE_BIN), "STATUS_BOARD_HOME": str(SB_HOME), "PYTHONDONTWRITEBYTECODE": "1", **link_env(data)}
    body = json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "source": "startup", "cwd": str(repo)})
    procs = [subprocess.Popen([sys.executable, "-c", wait, str(go), sys.executable, "-B", str(HOOK)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=environment, cwd=str(TMP)) for _ in range(6)]
    go.write_text("go")
    outs = [p.communicate(body, timeout=60)[0] for p in procs]
    assert sum(out.count("<board-link-offer>") for out in outs) == 1, [o[:40] for o in outs]


def test_board_link_offer_is_silent_where_it_must_be_and_does_not_use_up_the_once():
    repo = board_project()
    data = fresh_data()
    no_board = project("notes", "new")
    plain = TMP / "not-a-project"
    plain.mkdir(exist_ok=True)
    cases = {
        "a worker, by its first prompt": dict(cwd=repo, transcript=worker_transcript()),
        "a worker, by a live dispatch": dict(cwd=repo, orca_mode="live"),
        "an agent_type": dict(cwd=repo, payload={"agent_type": "reviewer"}),
        "a teammate_name": dict(cwd=repo, payload={"teammate_name": "dev"}),
        "a pane teammate's launch flags": dict(cwd=repo, ancestor_flags=("--agent-id=a1",)),
        "claude -p (attended 0)": dict(cwd=repo, env=link_env(data, CLAUDE_CODE_SESSION_ATTENDED="0")),
        "claude -p (sdk entrypoint)": dict(cwd=repo, env=link_env(data, CLAUDE_CODE_ENTRYPOINT="sdk-cli")),
        "resume": dict(cwd=repo, source="resume"),
        "compact": dict(cwd=repo, source="compact"),
        "clear": dict(cwd=repo, source="clear"),
        "a project without a board": dict(cwd=no_board),
        "a folder that is not a project": dict(cwd=plain),
        "PROJECT_INIT_NO_OFFER=1": dict(cwd=repo, env=link_env(data, PROJECT_INIT_NO_OFFER="1")),
    }
    for label, kwargs in cases.items():
        kwargs = dict(kwargs)
        kwargs["env"] = kwargs.get("env") or link_env(data)
        cwd = kwargs.pop("cwd")
        orca = kwargs.pop("orca", True)
        result = run_hook(cwd, orca=orca, **kwargs)
        assert "<board-link" not in result.out and result.code == 0 and not result.err, f"{label}: {said(result)}"
        assert not (data / "board-link.offered").exists(), f"{label}: a session that is not the owner's used up the once"
    unset = run_hook(repo, orca=False, env=link_env(None))
    assert "<board-link-offer>" not in unset.out, "no CLAUDE_PLUGIN_DATA (older Claude Code): there is nowhere to keep the once, so nothing is said"
    for answer in ("row", "status-line", "both", "declined"):
        asked = fresh_data()
        asked.mkdir()
        (asked / "board-link.json").write_text(json.dumps({"v": 1, "answer": answer}))
        assert run_hook(repo, orca=False, env=link_env(asked)).silent, f"after record ({answer}) it is never raised again"
    assert "<board-link-offer>" in run_hook(repo, orca=False, env=link_env(data)).out, "and the silent sessions left the once for the owner"


def test_the_board_link_block_writes_only_in_the_data_folder():
    repo, data = board_project(), fresh_data()
    config = TMP / "config-w"
    settings_for(config, "echo hello")
    def snapshot() -> dict:
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for root in (TMP, PLUGIN / "hooks", PLUGIN / "status-board")
                for p in root.rglob("*") if p.is_file() and "calls.log" not in p.name and ".git/" not in str(p)
                and data not in p.parents and not p.name.startswith("transcript-")}
    before = snapshot()
    run_hook(repo, orca=False, env=link_env(data, config))
    wired_record(data)
    run_hook(repo, orca=False, env=link_env(data, config))
    assert snapshot() == before, "nothing outside the plugin's data folder was written (settings.json, the board, the project, the plugin)"
    made = {p.name for p in data.iterdir()} - {"board-link.json", "statusline-before.json"}  # the two the test wrote itself
    assert "board-link.offered" in made and all(n == "board-link.offered" or n.startswith("board-link.rewrite-") for n in made), sorted(made)
    assert not [p for p in data.rglob("*.tmp")], "a temporary file and a rename: none left behind"


STATUS_LINE_LOST = "<board-link-statusline>"


def test_the_statusline_line_when_the_command_no_longer_carries_the_link_once_per_change():
    repo, data, config = board_project(), fresh_data(), TMP / "config-r"
    wired_record(data)
    settings_for(config, "echo hello")
    first = run_hook(repo, orca=False, env=link_env(data, config))
    assert STATUS_LINE_LOST in first.out and first.out.rstrip().endswith("</board-link-statusline>"), said(first)
    for phrase in ("no longer carries the board's link", "above the prompt", "/project-init:board-link", "one sentence"):
        assert phrase in first.out, f"missing {phrase!r}:\n{first.out}"
    # The button above the prompt is drawn whenever either switch is on, so the line says that it is still there: no rule about when a row comes back.
    assert "is back above the prompt" not in first.out and "ten minutes" not in first.out and "comes back" not in first.out, f"the old rule of the row:\n{first.out}"
    assert "button above the prompt is still there" in first.out and "whenever either board link switch is on" in first.out, f"it does not say that the button stays:\n{first.out}"
    assert "<board-link-offer>" not in first.out, "the owner was asked already: only the one line"
    assert run_hook(repo, orca=False, env=link_env(data, config)).silent, "once for this command"
    settings_for(config, "echo another command")
    assert STATUS_LINE_LOST in run_hook(repo, orca=False, env=link_env(data, config)).out, "a new rewrite is told again"
    settings_for(config, None)
    assert STATUS_LINE_LOST in run_hook(repo, orca=False, env=link_env(data, config)).out, "the status line removed altogether is a loss too"
    assert run_hook(repo, orca=False, env=link_env(data, config)).silent
    settings_for(config, f'w=${{COLUMNS:-}}; out=$(x); printf "%s" "$out" | python3 "$H/{MARKER}"')
    assert run_hook(repo, orca=False, env=link_env(data, config)).silent, "the wrapper is there: nothing to say"


def test_the_statusline_line_is_silent_unless_the_status_line_was_wired_and_the_switch_is_on():
    repo, config = board_project(), TMP / "config-s"
    settings_for(config, "echo hello")
    unwired = fresh_data()
    unwired.mkdir()
    (unwired / "board-link.json").write_text(json.dumps({"v": 1, "answer": "status-line"}))  # asked for, never added: no statusline-before.json
    assert run_hook(repo, orca=False, env=link_env(unwired, config)).silent, "never wired: the status line never carried the link"
    switched_off = fresh_data()
    wired_record(switched_off)
    settings_for(config, "echo hello", status_line_on=False)
    assert run_hook(repo, orca=False, env=link_env(switched_off, config)).silent, "the owner turned the switch off"
    settings_for(config, "echo hello")
    quiet = fresh_data()
    wired_record(quiet)
    for label, kwargs in {"a worker": dict(transcript=worker_transcript()), "an agent": dict(payload={"agent_id": "a"}), "resume": dict(source="resume"),
                          "clear": dict(source="clear"), "claude -p": dict(env=link_env(quiet, config, CLAUDE_CODE_SESSION_ATTENDED="0"))}.items():
        kwargs.setdefault("env", link_env(quiet, config))
        assert run_hook(repo, orca=True, **kwargs).silent, f"{label}: {said(run_hook(repo, orca=True, **kwargs))}"
    assert run_hook(no_board_project(), orca=False, env=link_env(quiet, config)).silent, "a project without a board"
    wrecked = fresh_data()
    wired_record(wrecked)
    broken = TMP / "config-broken"
    broken.mkdir(exist_ok=True)
    (broken / "settings.json").write_text("{ not json")
    result = run_hook(repo, orca=False, env=link_env(wrecked, broken))
    assert result.silent, f"a settings.json that is not JSON: nothing said, no error: {said(result)}"


def no_board_project() -> Path:
    return project("notes", "new")


def test_the_two_blocks_are_independent():
    data = fresh_data()
    bare = project("shop-app")  # no operating model
    with_board(bare)
    both = run_hook(bare, env=link_env(data))
    assert both.out.index("<bootstrap-offer>") < both.out.index("<board-link-offer>"), f"the bootstrap's block first, then the board link's:\n{both.out}"
    modeled = board_project()
    only_link = run_hook(modeled, env=link_env(fresh_data()))
    assert "<board-link-offer>" in only_link.out and "<bootstrap-offer>" not in only_link.out
    opted_out = project("shop-app")
    with_board(opted_out)
    (opted_out / ".claude").mkdir(exist_ok=True)
    (opted_out / ".claude" / "no-bootstrap-offer").write_text("")
    mixed = run_hook(opted_out, env=link_env(fresh_data()))
    assert "<bootstrap-offer>" not in mixed.out and "<board-link-offer>" in mixed.out, "the per-project file is about the bootstrap, not about the link"
    off = run_hook(bare, env=link_env(fresh_data(), PROJECT_INIT_NO_OFFER="1"))
    assert off.silent, "the variable that stops the offers stops both"


def test_a_failure_of_the_board_link_block_never_costs_the_bootstrap_offer_or_the_session():
    bare = project("shop-app")
    with_board(bare)
    blocker = TMP / "a-file-not-a-folder"
    blocker.write_text("x")
    for data in (blocker / "inside", TMP / "no-write" / "data"):  # a path under a file, and a folder nobody may write in
        if data.parent.name == "no-write":
            data.parent.mkdir(exist_ok=True)
            data.parent.chmod(0o500)
        try:
            result = run_hook(bare, env=link_env(data))
        finally:
            if data.parent.name == "no-write":
                data.parent.chmod(0o755)
        assert result.code == 0 and not result.err and "Traceback" not in result.out, said(result)
        assert result.offered, f"the bootstrap offer is still made when the data folder cannot be written: {said(result)}"
    broken = TMP / "hooks-no-setup"
    shutil.copytree(PLUGIN, broken, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    (broken / "status-board" / "statusline_setup.py").write_text("def broken(:\n")
    wired = fresh_data()
    wired_record(wired)
    config = TMP / "config-x2"
    settings_for(config, "echo hello")
    result = run_hook(bare, hooks_dir=broken / "hooks", env=link_env(wired, config))
    assert result.code == 0 and not result.err and "Traceback" not in result.out and result.offered, said(result)


def test_hooks_json_names_the_board_link_lines():
    config = json.loads((HOOKS_DIR / "hooks.json").read_text())
    number, entry = next((n, g) for n, g in enumerate(config["hooks"]["SessionStart"]) if "offer-bootstrap.py" in json.dumps(g))
    description = hooks_readme_text("SessionStart", number)
    assert "board-link-offer" in description and "board-link-statusline" in description, description[:200]
    assert entry["hooks"][0]["timeout"] <= 10


# ── two plugins in one project (the owner's ruling of 19 Nov 2026) ───
# With the orca-coordinator plugin's project part in the project, the offer says only what the bootstrap would add. The hook
# looks for the file and nothing else: it never asks `claude plugin list`. Alone, the offer and the note are what 2.5.0 said.
PART = Path(".claude") / "orca-coordinator" / "project.md"
VALID_PART = ("register: docs/workstreams/README.md\nreviews: costly-faults\nblock: block\n\n"
              "## How this project differs\n\nThe user's own lines.\n")

# What 2.5.0 printed, word for word, with the project's root where it names it (kept here, not read back from the hook).
ALONE_OFFER = (
    "<bootstrap-offer>\n"
    "This project ({root}) has no Orca operating model: no .claude/rules/operating-model.md here or in its main checkout. "
    "Before other work, offer the owner /project-bootstrap in one or two sentences: it writes a few files on how this project "
    "is run under Orca (the worker roster, the Task title convention, when an independent check is required, the workstream "
    "register). Ask what the project is first, and create or change no file until the owner says yes. If they decline, carry "
    "on and do not raise it again this session.\n"
    "To stop this offer: an empty .claude/no-bootstrap-offer in this project (only if the owner asks you to), or "
    "PROJECT_INIT_NO_OFFER=1 everywhere.\n"
    "</bootstrap-offer>\n"
)
OLD_SET_NOTE = (
    "<bootstrap-update>\n"
    "This project ({root}) has the older project-bootstrap set (.claude/rules/team-roles.md), written for Claude Code agent "
    "teams. The plugin now writes an Orca-only set. Tell the owner in one sentence, once, that /project-bootstrap update can "
    "show what it would add and retire, and that the old set's tree lock no longer runs, so until the update it is one writing "
    "agent at a time. Do not edit those files yourself.\n"
    "To stop this note: an empty .claude/no-bootstrap-offer in this project, or PROJECT_INIT_NO_OFFER=1.\n"
    "</bootstrap-update>\n"
)
WORKER_WORDS = ("roster", "register", "Task title", "independent check", "worker-start", "task-spec", "worktree")


def put_part(root: Path, text: str = VALID_PART) -> Path:
    path = root / PART
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def assert_alone(result: Result, repo: Path, label: str) -> None:
    assert result.code == 0 and not result.err, f"{label}: {said(result)}"
    assert result.out == ALONE_OFFER.format(root=repo), f"{label}: not the 2.5.0 offer:\n{result.out}"


def assert_found(result: Result, repo: Path, label: str) -> None:
    assert result.code == 0 and not result.err and result.offered and not result.noted, f"{label}: {said(result)}"
    assert result.out != ALONE_OFFER.format(root=repo), f"{label}: the alone offer, with the project part there"
    assert result.out.count("<bootstrap-offer>") == 1 and result.out.count("</bootstrap-offer>") == 1, result.out
    assert f"({repo})" in result.out, f"{label}: the offer names the project's root"


def test_the_alone_offer_and_the_old_set_note_are_word_for_word_as_at_2_5_0():
    repo, old = project("shop-app"), project("notes", "old")
    assert_alone(run_hook(repo), repo, "no scaffold, no project part")
    assert_alone(run_hook(repo, source="clear"), repo, "clear")
    assert run_hook(old).out == OLD_SET_NOTE.format(root=old), run_hook(old).out
    put_part(old)  # the note is not changed by the project part: it is about the old set
    assert run_hook(old).out == OLD_SET_NOTE.format(root=old), "the older-set note, with the project part there"
    child = worktree(project("blog"))
    assert run_hook(child).out == ALONE_OFFER.format(root=child), "a worktree with no scaffold anywhere"


def test_the_offer_with_and_without_the_project_part_at_each_of_the_three_places():
    """The places the hook already looks for the operating model: the cwd, its git top-level, the main checkout."""
    # the cwd: a sub-folder holds the part
    repo = project("shop-app")
    sub = repo / "pkg"
    sub.mkdir()
    assert_alone(run_hook(sub), repo, "cwd, before the part is there")
    put_part(sub)
    assert_found(run_hook(sub), repo, "the part at the cwd")
    # the git top-level: the cwd is a folder below it
    repo = project("shop-app")
    deep = repo / "src" / "app"
    deep.mkdir(parents=True)
    assert_alone(run_hook(deep), repo, "top, before the part is there")
    put_part(repo)
    assert_found(run_hook(deep), repo, "the part at the git top-level")
    assert_found(run_hook(repo), repo, "the part at the git top-level, from the top")
    # the main checkout: a worktree whose branch does not carry the part (it is not committed)
    main = project("shop-app")
    child = worktree(main)
    assert_alone(run_hook(child), child, "main checkout, before the part is there")
    put_part(main)
    assert not (child / PART).exists(), "the part is only in the main checkout"
    assert_found(run_hook(child), child, "the part in the main checkout only")
    # a part that is committed travels into every worktree
    committed = project("blog")
    put_part(committed)
    git(committed, "add", "-A")
    git(committed, "commit", "-q", "-m", "part")
    wt = worktree(committed)
    assert (wt / PART).is_file(), "a committed part is in the worktree"
    assert_found(run_hook(wt), wt, "a committed part, seen from a worktree")


def test_the_found_offer_says_only_what_the_bootstrap_would_add():
    repo = project("shop-app")
    put_part(repo)
    for source in ("startup", "clear"):
        out = run_hook(repo, source=source).out
        assert out.startswith("<bootstrap-offer>\n") and out.rstrip().endswith("</bootstrap-offer>"), out
        for phrase in ("no Orca operating model", ".claude/rules/operating-model.md", "main checkout", "/project-bootstrap", "orca-coordinator",
                       "verification", "release", "status board", "Ask what the project is first", "no file until",
                       "do not raise it again this session", ".claude/no-bootstrap-offer", "PROJECT_INIT_NO_OFFER=1"):
            assert phrase in out, f"{source}: missing {phrase!r} in:\n{out}"
        for word in WORKER_WORDS:
            assert word.lower() not in out.lower(), f"{source}: the found offer offers a worker part again ({word!r}):\n{out}"
    assert len(run_hook(repo).out.splitlines()) <= 5, "the offer stays a short block"


def test_an_invalid_or_empty_project_part_is_not_a_sign():
    """Valid is how orca-coordinator reads the file (hooks/state.sh oc_setting): `key: value` lines before the line
    `## How this project differs`, one of its own keys with a value it accepts. An empty file, prose, other keys, a value it
    would ignore, or a line only after the stop line count for nothing."""
    cases = {
        "an empty file": "",
        "blank lines": "\n\n  \n",
        "prose": "This project is run with another plugin.\nNothing else here.\n",
        "keys that are not its own": "model: opus\ntool.executor: claude\nformat: 1\n",
        "a key in another letter case": "Register: docs/workstreams/README.md\n",
        "an indented key": "  register: docs/workstreams/README.md\n",
        "an empty value": "register:\nreviews:   \n",
        "a reviews value it does not know": "reviews: sometimes\n",
        "a block value it does not know": "block: maybe\n",
        "a capability value it does not know": "capability.executor: anything\n",
        "a key after the stop line": "format: 1\n\n## How this project differs\nregister: docs/workstreams/README.md\n",
        "only the stop line and prose": "## How this project differs\n\nOur words.\n",
        "a colon in prose, no key": "Note: this is about the project\n",
    }
    for label, text in cases.items():
        repo = project("shop-app")
        put_part(repo, text)
        assert_alone(run_hook(repo), repo, label)
    for label, make in (("a folder in its place", lambda r: (r / PART).mkdir(parents=True)),
                        ("a file too large to be a project part", lambda r: put_part(r, "register: a.md\n" + "x" * 200_000))):
        repo = project("shop-app")
        make(repo)
        assert_alone(run_hook(repo), repo, label)
    repo = project("shop-app")  # the folder of the plugin, and no file in it
    (repo / PART).parent.mkdir(parents=True)
    (repo / ".claude" / "orca-coordinator" / "roles").mkdir()
    (repo / ".claude" / "orca-coordinator" / "roles" / "executor.md").write_text("register: docs/workstreams/README.md\n")
    assert_alone(run_hook(repo), repo, "a role file and no project part")


def test_a_valid_project_part_in_each_of_its_forms_is_a_sign():
    cases = {
        "the plugin's own template": VALID_PART,
        "its register line alone": "register: work/register.md\n",
        "format line first": "format: 1\nregister: work/register.md\n",
        "reviews alone": "reviews: every-change\n",
        "block alone": "block: tell\n",
        "balance alone": "balance.roles: executor, verifier\n",
        "a capability alone": "capability.qa-tester: trusted\n",
        "other keys first": "model: opus\nformat: 1\nreviews: costly-faults\n",
        "windows line ends": "format: 1\r\nblock: block\r\n",
        "a value with spaces around it": "block:    tell   \n",
    }
    for label, text in cases.items():
        repo = project("shop-app")
        put_part(repo, text)
        assert_found(run_hook(repo), repo, label)


def test_the_registers_marker_line_alone_is_not_a_sign():
    repo = project("shop-app")
    register = repo / "docs" / "workstreams" / "README.md"
    register.parent.mkdir(parents=True)
    register.write_text("<!-- orca-coordinator register -->\n# Workstream Register\n\n| ID | Workstream | Status | Next step | Forecast | Record |\n|----|---|---|---|---|---|\n")
    assert_alone(run_hook(repo), repo, "the marker line in the register")
    (repo / ".claude" / "orca-coordinator").mkdir(parents=True)
    assert_alone(run_hook(repo), repo, "and the plugin's folder with no project part")
    put_part(repo)
    assert_found(run_hook(repo), repo, "the project part makes it a sign, with or without the register")


def test_with_the_project_part_there_every_other_gate_holds_as_before():
    repo = project("shop-app")
    put_part(repo)
    assert_found(run_hook(repo), repo, "the owner's own session")
    silent = {
        "resume": run_hook(repo, source="resume"), "compact": run_hook(repo, source="compact"), "fork": run_hook(repo, source="fork"),
        "outside Orca": run_hook(repo, orca=False), "opt-out variable": run_hook(repo, env={"PROJECT_INIT_NO_OFFER": "1"}),
        "a worker, by its first prompt": run_hook(repo, transcript=worker_transcript()),
        "a worker, by a live dispatch": run_hook(repo, orca_mode="live"),
        "an agent_type": run_hook(repo, payload={"agent_type": "reviewer"}),
        "a teammate flag": run_hook(repo, ancestor_flags=("--agent-id=a1",)),
        "claude -p": run_hook(repo, env={"CLAUDE_CODE_SESSION_ATTENDED": "0"}),
        "the sdk": run_hook(repo, env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}),
        "not a project": run_hook(TMP),
    }
    for label, result in silent.items():
        assert result.silent, f"{label}: {said(result)}"
    modeled = project("notes", "new")
    put_part(modeled)
    assert run_hook(modeled).silent, "an operating model ends the offer, with the part there too"
    both = project("blog", "old")
    put_part(both)
    assert run_hook(both).out == OLD_SET_NOTE.format(root=both), "the older-set note comes before the found offer"
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "no-bootstrap-offer").write_text("")
    assert run_hook(repo).silent, "the opt-out file"
    assert run_hook(worktree(repo)).silent, "the opt-out file, by the main checkout"


def test_the_hook_looks_for_the_file_only_and_never_asks_for_the_plugin_list():
    log = FIXTURES / "claude.log"
    log.unlink(missing_ok=True)
    alone, found = project("shop-app"), project("notes")
    put_part(found)
    for repo in (alone, found, worktree(found)):
        run_hook(repo)
        run_hook(repo, orca_mode="live")
    assert not log.exists(), f"the hook ran claude: {log.read_text() if log.exists() else ''}"
    source = HOOK.read_text()
    assert "plugin list" not in source.replace("`claude plugin list`", ""), "the hook has no call of the plugin list"
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert '"claude"' not in code and "'claude'" not in code, "the hook names no claude command"


def test_a_project_part_that_is_not_a_plain_file_cannot_hang_the_hook():
    repo = project("shop-app")
    (repo / PART).parent.mkdir(parents=True)
    os.mkfifo(repo / PART)
    result = run_hook(repo, timeout=8)  # a read of a FIFO blocks: the hook stats the file first
    assert_alone(result, repo, "a FIFO in place of the project part")
    assert result.seconds < 3, f"{result.seconds:.1f} s"
    linked = project("blog")
    target = TMP / "elsewhere-part.md"
    target.write_text(VALID_PART)
    (linked / PART).parent.mkdir(parents=True)
    (linked / PART).symlink_to(target)
    assert_found(run_hook(linked), linked, "a link to a plain file is a plain file, as orca-coordinator reads it")


def test_the_hooks_readme_says_the_offer_is_smaller_with_the_project_part():
    config = json.loads((HOOKS_DIR / "hooks.json").read_text())
    number = next(n for n, g in enumerate(config["hooks"]["SessionStart"]) if "offer-bootstrap.py" in json.dumps(g))
    text = (HOOKS_DIR / "README.md").read_text()  # the whole section: its last paragraph is the group's own text, pinned by test_hooks_json
    block = next(b for b in re.split(r"(?m)^## ", text)[1:] if b.split()[0] == f"SessionStart[{number}]")
    low = " ".join(block.split()).lower()
    assert "orca-coordinator" in low and ".claude/orca-coordinator/project.md" in low, low[:400]
    assert "plugin list" in low and ("never" in low or "does not" in low), "it says the hook does not ask the plugin list"


# ── runner ───────────────────────────────────────────────────────────
def main() -> int:
    setup()
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f) and ARGS.only in n]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as exc:  # noqa: BLE001 - a test runner reports every failure
                failed += 1
                reason = " ".join(str(exc).split())[:230] or type(exc).__name__
                print(f"FAIL  {name}: {type(exc).__name__}: {reason}")
    finally:
        for path in TMP.rglob("*"):
            try:
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed} passed, {failed} failed, plugin under test: {PLUGIN}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
