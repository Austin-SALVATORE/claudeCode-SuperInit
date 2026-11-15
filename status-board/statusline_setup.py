#!/usr/bin/env python3
"""Put the board's link into a user's status line, or take it out again (release 2.3.0, 15 Nov 2026).

    python3 statusline_setup.py status
    python3 statusline_setup.py add [--replace-hand-made] [--dry-run]
    python3 statusline_setup.py remove [--dry-run]
    python3 statusline_setup.py record --answer row|status-line|both|declined [--hud proposed|declined]
    (all: [--config-dir DIR], else CLAUDE_CONFIG_DIR, else ~/.claude)

The command line is fixed by statusline/option/INTERFACE.md. Each command prints **exactly one line of JSON** and nothing
else on stdout. Exit 0 when it did what was asked (a no-op included), 2 when it refuses (nothing was written; the JSON has
`"refused": "<the reason in one sentence>"`, and a short machine-readable `"code"`), 1 on anything unexpected (nothing
half-written; `"error": "<what>"`).

* `status` (writes nothing): `has_status_line`, `is_claude_hud` (the command contains `claude-hud`, the test claude-hud's own
  doctor uses), `wrapped` (our file name, `statusline-board-link.py`, is in the command), `hand_made_filter` (the command
  pipes through a hand-made board filter from before 2.3.0: a script named `hud-board-link.py` that some set-ups pipe their
  status line through), `claude_hud_installed` (Claude Code's install record lists a
  claude-hud plugin), `recorded` (the content of board-link.json in the data folder, or null).
* `add`: with `--dry-run` it writes nothing and prints `{"old", "new"}`, the two texts the skill shows before the user's yes
  (a refusal is reported the same way without `--dry-run`, so the skill learns it before it asks). Without it, it writes and
  prints `{"old", "new", "backup", "written": true}`; with the wrapper already there, `{"written": false, "already": true}`.
* `remove [--dry-run]`: restores the old command exactly and prints `{"old", "new", "written": true}`; with nothing of ours in
  the command, `{"written": false, "already": true}`. It takes the launcher's copy out of the data folder.
* `record`: writes board-link.json in the data folder (a temporary file and a rename), overwriting it (the user may run the
  skill again and choose differently), and prints what it wrote.

**This is the only code of the plugin that edits a user's settings.json, and the session that runs it does so only after
showing the old and the new command and getting the user's yes** (skills/board-link). A hook never runs it. It edits the
file's TEXT in place, so that nothing else in the file moves and `remove` gives the file back byte for byte. **It never
touches the plugin's two switches** (`board_link_row`, `board_link_status_line`): the skill sets them, with
`claude plugin configure`.

`add` makes the status line command `ORIG` into (claude-hud's own: `ORIG` contains `claude-hud`):

    w=${COLUMNS:-}; out=$([ "$w" -gt 20 ] 2>/dev/null && export COLUMNS=$((w - 8)); ORIG); rc=$?; if [ -n "$out" ]; then \\
        printf '%s\\n' "$out" | BOARD_LINK_COLUMNS="$w" python3 -I -S -B "<data folder>/statusline-board-link.py" 2>/dev/null \\
        || printf '%s\\n' "$out"; fi; exit $rc

and for any other status line the same without `[ "$w" -gt 20 ] 2>/dev/null && export COLUMNS=$((w - 8));`. The wrapped
command's exit status is the wrapper's (`rc`), and when it printed nothing the wrapper prints nothing (the first build ended with
the status of its last printf and printed an empty line); a command written in that earlier form is still recognised. ORIG is kept
verbatim (never parsed), so `hud doctor` still finds `claude-hud` in it. The export is inside `$( )`: ORIG alone gets 8
columns fewer (the width of ` | board`), and the caller's COLUMNS is unchanged. Python missing, the plugin gone, any crash:
the `||` prints the lines as they came. Other keys of `statusLine` are kept. The launcher (statusline_launcher.py) is copied
into the data folder first, so the command never points at a file that is not there, and the old command is kept in
`statusline-before.json` (its existence also tells the session-start hook that this status line was wired once).

`add` refuses, writing nothing, when there is no `statusLine` command; when ORIG has a newline or a `#` (a comment would
swallow the closing `)`); for a Windows PowerShell form; when the composed command does not parse under bash (a bare `case`
with its `)` patterns, inside `$( )`, is a syntax error for the bash 3.2 macOS ships); when a hand-made board filter
from before 2.3.0 (a script named `hud-board-link.py`) is in it, unless `--replace-hand-made`, which swaps only the path of that script for the plugin's
launcher (same place, same words); and when the command does not parse on its own under bash (`bash -n`: a stray `)` would close
the wrapper's `$(` and leave the rest of the command outside it). The file is written the way claude-hud's own installer writes it:
the real file behind a symlink, the same permissions, a temporary file and a rename, after a copy `settings.json.bak.<time>` made
beside the real file; two `add` at once give one wrapper (a lock in the data folder).

**Modes** (settings.json can hold tokens in `env`; the review of 15 Nov 2026): the backup and the rewritten settings.json have
exactly the original's mode, and are never open to more than that for an instant: each is a new file created closed (0600, O_EXCL,
O_NOFOLLOW, so nothing already at the name is followed or overwritten: the next free name is taken), written, and given its mode
by fchmod last (statusline_filter.open_new, write_private). statusline-before.json holds the old command: 0600 (the original's
owner bits when it has fewer). board-link.json, the lock and, in the filter, the cache and the seen marks: 0600 in folders of 0700.
The launcher is a copy of a public file and keeps that file's mode (0644 at most). `remove` checks the command against what `add` writes (the text between the known
prefix and suffix) and refuses, writing nothing, when the marker is there but the command was edited.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from statusline_filter import DATA_ID, open_new, write_private  # noqa: E402 - where the data folder is; how a private file is made

PLUGIN_KEY = "project-init@claudecode-superinit"
MARKER = "statusline-board-link.py"
HAND_MADE = "hud-board-link.py"
OPTION_STATUS_LINE = "board_link_status_line"
ANSWERS = ("row", "status-line", "both", "declined")
HUD_ANSWERS = ("proposed", "declined")

PREFIX_REDUCING = 'w=${COLUMNS:-}; out=$([ "$w" -gt 20 ] 2>/dev/null && export COLUMNS=$((w - 8)); '
PREFIX_PLAIN = "w=${COLUMNS:-}; out=$("
LAUNCHER_REF = f'"${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/plugins/data/{DATA_ID}/{MARKER}"'
LINK_PIPE = f"printf '%s\\n' \"$out\" | BOARD_LINK_COLUMNS=\"$w\" python3 -I -S -B {LAUNCHER_REF} 2>/dev/null || printf '%s\\n' \"$out\""
# The wrapped command's exit status is kept (`rc`), and nothing is printed when it printed nothing (review of 15 Nov 2026, finding 5).
SUFFIX = f"); rc=$?; if [ -n \"$out\" ]; then {LINK_PIPE}; fi; exit $rc"
# What the first 2.3.0 build wrote: it ended with the status of its last printf and printed an empty line for no output. A settings.json
# written by it still holds this form, so `status`, `add` and `remove` still recognise it.
EARLIER_SUFFIX = f"); {LINK_PIPE}"
POWERSHELL = re.compile(r"cmd\.exe\s+/d\s+/s\s+/c|powershell", re.IGNORECASE)
HAND_MADE_PATH = re.compile(r"\"[^\"\n]*" + re.escape(HAND_MADE) + r"\"|'[^'\n]*" + re.escape(HAND_MADE) + r"'|[^\s\"']*" + re.escape(HAND_MADE))


class Refused(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


# ── the settings file, as text ───────────────────────────────────────────────────────────────────────────────────────
def _ws(t: str, i: int) -> int:
    while i < len(t) and t[i] in " \t\r\n":
        i += 1
    return i


def _string_end(t: str, i: int) -> int:
    i += 1
    while i < len(t):
        if t[i] == "\\":
            i += 2
        elif t[i] == '"':
            return i + 1
        else:
            i += 1
    raise ValueError("unterminated string")


class JsonText:
    """A JSON document kept as text, with the spans of its object members, so that one value can be replaced without
    touching a byte elsewhere. (json.dumps of a parsed file would reindent it: it would not give the file back.)"""

    def __init__(self, text: str) -> None:
        self.bom = "﻿" if text.startswith("﻿") else ""
        self.text = text[len(self.bom):]
        self.spans: dict[tuple, tuple[int, int]] = {}
        end = self._scan(_ws(self.text, 0), ())
        if _ws(self.text, end) != len(self.text):
            raise ValueError("text after the document")
        self.value = json.loads(self.text)

    def _scan(self, i: int, path: tuple) -> int:
        t = self.text
        c = t[i]
        if c == "{":
            start, i = i, _ws(t, i + 1)
            if t[i] == "}":
                self.spans[path] = (start, i + 1)
                return i + 1
            while True:
                i = _ws(t, i)
                key_end = _string_end(t, i)
                key = json.loads(t[i:key_end])
                i = _ws(t, key_end)
                if t[i] != ":":
                    raise ValueError("expected a colon")
                i = _ws(t, self._scan(_ws(t, i + 1), path + (key,)))
                if t[i] == ",":
                    i += 1
                    continue
                if t[i] == "}":
                    self.spans[path] = (start, i + 1)
                    return i + 1
                raise ValueError("expected , or }")
        if c == "[":
            i = _ws(t, i + 1)
            if t[i] == "]":
                return i + 1
            while True:
                i = _ws(t, self._scan(_ws(t, i), ("\0array",)))
                if t[i] == ",":
                    i += 1
                    continue
                if t[i] == "]":
                    return i + 1
                raise ValueError("expected , or ]")
        if c == '"':
            end = _string_end(t, i)
        else:
            end = i
            while end < len(t) and t[end] not in ",}] \t\r\n":
                end += 1
            if end == i:
                raise ValueError("unexpected character")
        self.spans[path] = (i, end)
        return end

    def literal(self, path: tuple) -> str | None:
        span = self.spans.get(path)
        return self.text[span[0]:span[1]] if span else None

    def replace(self, path: tuple, literal: str) -> "JsonText":
        start, end = self.spans[path]
        return JsonText(self.bom + self.text[:start] + literal + self.text[end:])

    def dump(self) -> str:
        return self.bom + self.text


# ── where things are ─────────────────────────────────────────────────────────────────────────────────────────────────
def config_dir(explicit: str | None) -> Path:
    return Path(explicit or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def data_dir(config: Path) -> Path:
    """The plugin's data folder for a marketplace install: the folder the command names and the launcher lives in."""
    return config / "plugins" / "data" / DATA_ID


def read_settings(config: Path) -> JsonText | None:
    """The settings as text, or None when there is no settings.json. Raises Refused when it cannot be read as a JSON object."""
    path = config / "settings.json"
    if not path.exists():
        return None
    try:
        text = path.read_bytes().decode("utf-8")
        if not (text[1:] if text.startswith("﻿") else text).strip(" \t\r\n"):
            return None  # an empty file (or only blanks) holds no settings: the same as no file, for status, add and remove
        doc = JsonText(text)
    except (OSError, ValueError, IndexError) as exc:  # IndexError: a document cut off in the middle (`{`, `{"a":`)
        raise Refused("settings-unreadable", f"settings.json cannot be read as JSON ({exc.__class__.__name__}); nothing was changed") from exc
    if not isinstance(doc.value, dict):
        raise Refused("settings-unreadable", "settings.json does not hold a JSON object; nothing was changed")
    return doc


def status_line_command(doc: JsonText | None) -> str | None:
    line = doc.value.get("statusLine") if doc is not None else None
    if isinstance(line, dict) and line.get("type", "command") == "command" and isinstance(line.get("command"), str) and line["command"].strip():
        return line["command"]
    return None


def option_on(doc: JsonText | None, name: str, default: bool) -> bool:
    """A plugin option as Claude Code keeps it: pluginConfigs["<plugin>@<marketplace>"].options.<name>. Read only."""
    try:
        value = doc.value["pluginConfigs"][PLUGIN_KEY]["options"][name]
    except (AttributeError, KeyError, TypeError):
        return default
    return value if isinstance(value, bool) else default


def read_json_file(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text())
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        return None


def write_atomic(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """A file of this plugin in a folder of its own: made through a new, closed file and a rename (statusline_filter.write_private),
    never open to more than `mode`, which is 0600 unless the caller says the content is public."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, payload, mode, sync=True)


def settings_mode(config: Path) -> int:
    """The permission bits of the real file behind settings.json (it is often a symlink)."""
    return stat.S_IMODE(os.stat(os.path.realpath(config / "settings.json")).st_mode)


def write_settings(config: Path, text: str) -> str:
    """The new text over the real file behind settings.json (it is often a symlink), after a backup; returns the backup's path.

    The file can hold tokens (an `env` block), so neither the backup nor the new file is ever more open than the original was:
    the backup is a new file, created closed (0600, O_EXCL: a symlink or file at its name is never followed or overwritten, the
    next free name is taken), filled from the original, and given exactly the original's mode last; the new file goes through
    statusline_filter.write_private the same way, with the original's mode.

    The backup lies beside the REAL file when settings.json is a symlink (not beside the link): it is made where the file it
    copies lives, on the same filesystem and in the folder whose permissions already protect it, so it is no more exposed than
    the original; a dotfiles repository that holds the real file will see it as an untracked file, which is the price."""
    target = Path(os.path.realpath(config / "settings.json"))
    stamp = time.strftime("%Y%m%dT%H%M%S")
    with open(target, "rb") as source:
        mode = stat.S_IMODE(os.fstat(source.fileno()).st_mode)
        fd, copy = open_new(target.with_name(f"settings.json.bak.{stamp}"), mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                shutil.copyfileobj(source, handle)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), mode)
        except BaseException:
            try:
                copy.unlink()
            except OSError:
                pass
            raise
    write_private(target, text.encode("utf-8"), mode, sync=True)
    return str(copy)


def lock(data: Path):
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.fdopen(os.open(data / "statusline.lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600), "a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


# ── the command ──────────────────────────────────────────────────────────────────────────────────────────────────────
def compose(orig: str, reducing: bool, suffix: str = SUFFIX) -> str:
    return (PREFIX_REDUCING if reducing else PREFIX_PLAIN) + orig + suffix


def decompose(command: str) -> str | None:
    """ORIG when `command` is exactly what `add` writes (or wrote, in the earlier form), else None."""
    if not command.startswith(PREFIX_PLAIN):
        return None
    for suffix in (SUFFIX, EARLIER_SUFFIX):
        if not command.endswith(suffix):
            continue
        for reducing, prefix in ((True, PREFIX_REDUCING), (False, PREFIX_PLAIN)):
            if command.startswith(prefix):
                orig = command[len(prefix):-len(suffix)]
                if compose(orig, reducing, suffix) == command:
                    return orig
    return None


def bash_accepts(command: str) -> bool:
    bash = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")
    if bash is None:
        return True  # nothing to check with
    return subprocess.run([bash, "-n", "-c", command], capture_output=True, timeout=20).returncode == 0


def plan_add(doc: JsonText | None, replace_hand_made: bool) -> dict:
    """What `add` would write, or raises Refused. Nothing is written here."""
    command = status_line_command(doc)
    if command is None:
        raise Refused("no-status-line", "there is no status line command to add the link to; none is created")
    if MARKER in command:
        return {"already": True}
    if HAND_MADE in command:
        if not replace_hand_made:
            raise Refused("hand-made", "the status line already pipes through a hand-made board filter from before 2.3.0 (hud-board-link.py); --replace-hand-made swaps its path for the plugin's")
        if len(HAND_MADE_PATH.findall(command)) != 1 or command.count(HAND_MADE) != 1:
            raise Refused("hand-made-unreplaceable", "the hand-made filter is not named in one place that can be swapped; nothing was changed")
        new, form = HAND_MADE_PATH.sub(lambda _m: LAUNCHER_REF, command, count=1), "replace"
    else:
        if POWERSHELL.search(command):
            raise Refused("powershell", "a Windows PowerShell status line: the link is added only where the status line runs through bash")
        if "\n" in command or "\r" in command:
            raise Refused("newline", "the command has more than one line")
        if "#" in command:
            raise Refused("comment", "the command has a # (a comment would swallow the closing parenthesis)")
        form = "reducing" if "claude-hud" in command else "plain"
        new = compose(command, form == "reducing")
    if not bash_accepts(command):
        # Wrapped, a stray `)` closes the wrapper's `$(` and the composed text can parse while the rest of the user's command runs
        # OUTSIDE the substitution, beside the wrapper's own `exit` (the delta review of 15 Nov 2026): only a command that parses on
        # its own is wrapped.
        raise Refused("syntax", "the status line command does not parse by itself under bash (bash -n says so), so it is not wrapped: fix it first; nothing was changed")
    if not bash_accepts(new):
        raise Refused("syntax","the composed command does not parse under bash (a bare case with its ) patterns inside $( ) is a syntax error for macOS's bash 3.2)")
    return {"already": False, "old": command, "new": new, "form": form}


# ── the commands ─────────────────────────────────────────────────────────────────────────────────────────────────────
def out(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def refused(exc: Refused) -> int:
    return out({"refused": exc.reason, "code": exc.code}, 2)


def cmd_status(config: Path) -> int:
    data = data_dir(config)
    try:
        command = status_line_command(read_settings(config))
    except Refused:
        command = None  # settings that cannot be read: `add` will say so; status only reports what it can see
    try:
        installed = json.loads((config / "plugins" / "installed_plugins.json").read_text()).get("plugins", {})
        hud = any(key.startswith("claude-hud@") and bool(entries) for key, entries in installed.items())
    except (OSError, ValueError, AttributeError):
        hud = False
    return out({"has_status_line": command is not None, "is_claude_hud": command is not None and "claude-hud" in command,
                "wrapped": command is not None and MARKER in command, "hand_made_filter": command is not None and HAND_MADE in command,
                "claude_hud_installed": hud, "recorded": read_json_file(data / "board-link.json")})


def cmd_add(config: Path, dry_run: bool, replace_hand_made: bool) -> int:
    try:
        plan = plan_add(read_settings(config), replace_hand_made)
    except Refused as exc:
        return refused(exc)
    if plan["already"]:
        return out({"written": False, "already": True})
    if dry_run:
        return out({"old": plan["old"], "new": plan["new"]})
    data = data_dir(config)
    handle = lock(data)
    try:
        try:
            doc = read_settings(config)  # again, under the lock: another add may have been first
            plan = plan_add(doc, replace_hand_made)
        except Refused as exc:
            return refused(exc)
        if plan["already"]:
            return out({"written": False, "already": True})
        assert doc is not None
        source = HERE / "statusline_launcher.py"  # first: the command must never point at a file that is not there
        write_atomic(data / MARKER, source.read_bytes(), (stat.S_IMODE(source.stat().st_mode) & 0o644) | 0o400)  # a copy of a public file keeps its mode
        literal = doc.literal(("statusLine", "command"))
        new_doc = doc.replace(("statusLine", "command"), json.dumps(plan["new"], ensure_ascii=False))
        if status_line_command(new_doc) != plan["new"]:
            raise RuntimeError("the edited settings do not read back as intended")
        # The old command can hold a token (`API_KEY=… node …`): no more open than settings.json, and not open to others at all.
        write_atomic(data / "statusline-before.json", (json.dumps(
            {"v": 1, "form": plan["form"], "command": plan["old"], "command_literal": literal, "after_command": plan["new"]}) + "\n").encode(),
            settings_mode(config) & 0o600 or 0o600)
        backup = write_settings(config, new_doc.dump())
        return out({"old": plan["old"], "new": plan["new"], "backup": backup, "written": True})
    finally:
        handle.close()


def cmd_remove(config: Path, dry_run: bool) -> int:
    data = data_dir(config)
    try:
        doc = read_settings(config)
        command = status_line_command(doc)
        if command is None or MARKER not in command:
            return out({"written": False, "already": True})
        before = read_json_file(data / "statusline-before.json") or {}
        if before.get("after_command") == command:
            orig, literal = before["command"], before.get("command_literal")
        else:
            orig, literal = decompose(command), None
            if orig is None:
                raise Refused("not-ours", "the command has the wrapper's file name but is not the wrapper `add` writes (it was edited): remove it by hand")
    except Refused as exc:
        return refused(exc)
    if dry_run:
        return out({"old": command, "new": orig})
    handle = lock(data)
    try:
        new_doc = doc.replace(("statusLine", "command"), literal if literal is not None else json.dumps(orig, ensure_ascii=False))
        backup = write_settings(config, new_doc.dump())
        for leftover in (data / MARKER, data / "statusline-before.json"):
            try:
                leftover.unlink()
            except OSError:
                pass
        return out({"old": command, "new": orig, "backup": backup, "written": True})
    finally:
        handle.close()


def cmd_record(config: Path, answer: str | None, hud: str | None) -> int:
    if answer not in ANSWERS:
        return refused(Refused("bad-answer", f"--answer is one of {', '.join(ANSWERS)}"))
    if hud is not None and hud not in HUD_ANSWERS:
        return refused(Refused("bad-answer", f"--hud is one of {', '.join(HUD_ANSWERS)}"))
    record = {"v": 1, "answer": answer, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if hud is not None:
        record["hud"] = hud
    write_atomic(data_dir(config) / "board-link.json", (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    return out(record)


class UsageError(Exception):
    """A command line this script does not understand."""


class JsonArgumentParser(argparse.ArgumentParser):
    """argparse prints its usage on stderr and exits 2, which is no line of JSON and the exit code of a refusal; INTERFACE.md
    wants exactly one line of JSON for every command and exit 1 with `error` for anything unexpected (review of 15 Nov 2026)."""

    def error(self, message: str) -> None:
        raise UsageError(f"{self.prog}: {message}")


def parse_command_line() -> argparse.Namespace:
    ap = JsonArgumentParser(description="Put the board's link into a status line, or take it out.")
    ap.add_argument("--config-dir")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    add = sub.add_parser("add")
    add.add_argument("--dry-run", action="store_true")
    add.add_argument("--replace-hand-made", action="store_true")
    remove = sub.add_parser("remove")
    remove.add_argument("--dry-run", action="store_true")
    record = sub.add_parser("record")
    record.add_argument("--answer")
    record.add_argument("--hud")
    return ap.parse_args()


def main() -> int:
    try:
        args = parse_command_line()
    except UsageError as exc:
        return out({"error": f"usage: {exc}"}, 1)
    config = config_dir(args.config_dir)
    try:
        if args.command == "status":
            return cmd_status(config)
        if args.command == "add":
            return cmd_add(config, args.dry_run, args.replace_hand_made)
        if args.command == "remove":
            return cmd_remove(config, args.dry_run)
        return cmd_record(config, args.answer, args.hud)
    except Exception as exc:  # noqa: BLE001 - said as JSON; the atomic writes leave nothing half-written
        return out({"error": f"{exc.__class__.__name__}: {exc}"}, 1)


if __name__ == "__main__":
    sys.exit(main())
