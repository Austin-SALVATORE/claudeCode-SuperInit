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
    writers = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} & {
        "write_text", "write_bytes", "mkdir", "touch", "unlink", "rename", "replace", "open", "write_text", "chmod"}
    opens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"]
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


def test_hooks_json_describes_the_new_offer():
    config = json.loads((HOOKS_DIR / "hooks.json").read_text())
    entries = [group for group in config["hooks"]["SessionStart"] if "offer-bootstrap.py" in json.dumps(group)]
    assert len(entries) == 1, "the offer hook is registered once on SessionStart"
    description = entries[0]["description"]
    assert "operating-model.md" in description, description[:200]
    assert "Orca" in description and "team structure" not in description, description[:300]
    assert "CLAUDE_CODE_SESSION_ATTENDED" in description and "sdk" in description, "the runs nobody attends are named: " + description[:300]
    assert entries[0]["hooks"][0]["timeout"] <= 10, "a hook of every session start stays quick"


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
