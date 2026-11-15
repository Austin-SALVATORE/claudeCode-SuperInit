#!/usr/bin/env python3
"""The tree lock and its guard hook are retired, and everything that read them still works (15 Nov 2026).

    python3 tests/test_tree_lock_retired.py --plugin <plugin dir>

Under Orca the rule is "one writing worker per worktree" (the operating model), not
a lock that sees only `Agent`/`Task`/`SendMessage` and the old role names, so
hooks/tree-lock-guard.py and status-board/tree_lock.py are deleted. They have to go
together with their readers: with only the import gone from serve.py the server
failed at import time, and hooks/revive-board.py (which imports serve.py) with it;
with only the files gone, hooks.json named a script that does not exist, on every
Agent, Task and SendMessage call of every session.

What is tested: that the two files are gone; that serve.py and revive-board.py load
with `tree_lock` made unimportable; that the board's report has no `lock` row in
`state` (and that a `tree-lock.json` left behind in a board's folder is read by
nothing and not deleted); that hooks.json loads, has no PreToolUse group for
Agent/Task/SendMessage, and names only scripts that exist; and that no file of
hooks/ or status-board/ names the lock any more.

Everything runs in temporary folders with HOME and STATUS_BOARD_HOME pointing into
them, and a scratch git repository. No test starts a server or binds a port.

Run it against the code before the change and each test fails for the reason it
names (the files are there; `import tree_lock` is refused; `state` has a `lock` row;
hooks.json has the guard; the lock is named in five files).
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="the plugin directory under test")
ap.add_argument("--only", default="", help="run only the tests whose name contains this")
ARGS = ap.parse_args()
PLUGIN = Path(ARGS.plugin).resolve()
BOARD_DIR, HOOKS_DIR = PLUGIN / "status-board", PLUGIN / "hooks"
sys.dont_write_bytecode = True

TMP = Path(tempfile.mkdtemp(prefix="tree-lock-retired-")).resolve()
HOME, SB_HOME, REPO = TMP / "home", TMP / "board-home", TMP / "shop-app"
STALE_LOCK = {"held": True, "holder": "dev", "role": "dev", "grantedBy": "tree-lock-guard (auto)",
              "acquiredAt": "2026-11-14T10:00:00+01:00", "refreshedAt": "2026-11-14T10:05:00+01:00"}


def setup() -> None:
    for d in (HOME, SB_HOME / "boards" / "shop-app", REPO):
        d.mkdir(parents=True)
    git = ["git", "-C", str(REPO), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "scratch"], check=True, capture_output=True)
    board = SB_HOME / "boards" / "shop-app"
    (board / "narrative.json").write_text(json.dumps({"writtenAt": "2026-11-15T00:00:00+01:00", "headline": "h", "decisions": []}))
    (board / "tree-lock.json").write_text(json.dumps(STALE_LOCK))  # what a board that ran 2.1.0 or older may still hold


def python(code: str) -> subprocess.CompletedProcess:
    """A fresh interpreter, with the plugin's status-board/ on its path, in a scratch home."""
    env = {"HOME": str(HOME), "PATH": f"/usr/bin:/bin:{Path(sys.executable).parent}", "STATUS_BOARD_HOME": str(SB_HOME),
           "STATUS_BOARD_ORCA": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, env=env, cwd=str(TMP), timeout=60)


def last_line(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines() or [""])[-1][:200]


# ── the tests ─────────────────────────────────────────────────────────
def test_the_two_lock_files_are_gone():
    still_there = [str(p.relative_to(PLUGIN)) for p in (HOOKS_DIR / "tree-lock-guard.py", BOARD_DIR / "tree_lock.py") if p.exists()]
    assert not still_there, f"still in the plugin: {still_there}"


def test_serve_imports_without_tree_lock():
    # `sys.modules["tree_lock"] = None` makes any `import tree_lock` raise, whether or not the file exists.
    code = (f"import sys; sys.modules['tree_lock'] = None; sys.path.insert(0, {str(BOARD_DIR)!r})\n"
            "import serve\nprint('imported', hasattr(serve, 'slug_for'), hasattr(serve, 'port_for'), hasattr(serve, 'Board'))")
    proc = python(code)
    assert proc.returncode == 0 and proc.stdout.strip() == "imported True True True", f"exit {proc.returncode}: {last_line(proc)}"


def test_report_state_has_no_lock_row():
    code = (f"import json, sys; sys.path.insert(0, {str(BOARD_DIR)!r})\n"
            "from pathlib import Path\nimport serve\n"
            f"board = serve.Board(Path({str(REPO)!r}), None)\n"
            "report = board.report()\nprint(json.dumps(sorted(report['state'])))")
    proc = python(code)
    assert proc.returncode == 0, f"the report failed: {last_line(proc)}"
    rows = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "lock" not in rows, f"state still has a lock row: {rows}"
    assert rows == ["branch", "local", "tree", "unpushed", "upstream"], f"the other rows must stay as they were: {rows}"
    left = SB_HOME / "boards" / "shop-app" / "tree-lock.json"
    assert left.is_file() and json.loads(left.read_text()) == STALE_LOCK, "a tree-lock.json left in a board's folder is left alone"


def test_hooks_json_has_no_pretooluse_agent_guard():
    config = json.loads((HOOKS_DIR / "hooks.json").read_text())
    hooks = config["hooks"]
    for group in hooks.get("PreToolUse", []):
        matcher = group.get("matcher", "")
        assert not {"Agent", "Task", "SendMessage"} & set(re.split(r"[|,\s]+", matcher)), f"a PreToolUse group for {matcher!r}"
    assert "tree-lock-guard" not in json.dumps(config), "hooks.json still names the guard"
    # Nothing else was lost: the keeper's Bash hook is still the one PreToolUse entry, and the other events are as they were.
    assert [g.get("matcher") for g in hooks["PreToolUse"]] == ["Bash"], [g.get("matcher") for g in hooks["PreToolUse"]]
    assert set(hooks) == {"UserPromptSubmit", "SessionStart", "Stop", "PostToolUse", "PreToolUse"}, sorted(hooks)
    assert len(hooks["SessionStart"]) == 3 and len(hooks["PostToolUse"]) == 3 and len(hooks["UserPromptSubmit"]) == 2
    named = set(re.findall(r"hooks/([A-Za-z0-9_-]+\.py)", json.dumps(config)))
    missing = sorted(n for n in named if not (HOOKS_DIR / n).is_file())
    assert not missing and named, f"hooks.json names scripts that do not exist: {missing}"


def test_revive_board_loads_serve():
    code = ("import importlib.util, sys\nsys.modules['tree_lock'] = None\n"
            f"spec = importlib.util.spec_from_file_location('revive_board', {str(HOOKS_DIR / 'revive-board.py')!r})\n"
            "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            "serve = module.load_serve()\n"
            "print('loaded', callable(serve.slug_for), callable(serve.port_for))")
    proc = python(code)
    assert proc.returncode == 0 and proc.stdout.strip() == "loaded True True", f"exit {proc.returncode}: {last_line(proc)}"


def test_no_hook_or_board_file_names_the_lock_any_more():
    lock = re.compile(r"tree[_-]lock", re.IGNORECASE)
    found = []
    for root in (HOOKS_DIR, BOARD_DIR):
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                try:
                    lines = [i for i, text in enumerate(path.read_text(errors="replace").splitlines(), 1) if lock.search(text)]
                except OSError:
                    continue
                if lines:
                    found.append(f"{path.relative_to(PLUGIN)}:{lines[0]}")
    assert not found, f"still named in: {found}"


# ── runner ────────────────────────────────────────────────────────────
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
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{len(tests) - failed} passed, {failed} failed, plugin under test: {PLUGIN}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
