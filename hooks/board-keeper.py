#!/usr/bin/env python3
"""Record which session writes the board, so that only it is told the owner's answers.

The owner's ruling, 14 Nov 2026: "Only the session that last wrote the board's
content is told, within seconds. Other sessions in the folder stay silent. If you
start a new session and it takes over the board, it becomes the one that is told."
Claude Code reports the writer itself; the agent says nothing.

A SESSION IS RECORDED ONLY FOR A CHANGE ITS OWN TOOL CALL IS SEEN MAKING
(the review of 14 Nov 2026, findings 2 and 3; KEEPER-ATTRIBUTION.md). When in
doubt nobody is recorded and the keeper stays who it was.
  * `Write` and `Edit` (PostToolUse) to `.../boards/<slug>/narrative.json`: the
    tool's own input names the file, so the session is recorded. hooks.json gives
    these two groups an `if` rule, so every other Write or Edit costs nothing.
  * `Bash` whose command text names `narrative.json` (a `>` redirect included:
    the hook reads the command itself, not an `if` rule, which cannot see a
    redirect's target; spike of 14 Nov 2026, Claude Code 2.1.325). PreToolUse
    stores the file's signature in a bracket; PostToolUse closes it, and the
    session is recorded only if the file changed during that very call and no
    other session's call that also named the file could have changed it
    (answer_ledger.open_bracket / close_bracket). A `cat` that merely reads the
    file records nobody. hooks.json runs this for every Bash call behind a shell
    `case` that starts no Python unless the payload mentions the file: about
    6 ms per call, in every session.
  * Anything else records nobody: a script that writes the file without naming it
    (the skill says so), the owner's editor, serve.py, a worker, a headless run.
    There is no sweep at the end of the turn any more.

Spikes of 14 Nov 2026 (Claude Code 2.1.325): a Bash call's PreToolUse and
PostToolUse come in that order and carry the same `tool_use_id`, the same
`session_id` and `tool_input.command`; a subagent's carry the LEAD's session_id
plus its own agent_id; `cwd` follows a `cd` made in a Bash call.

WHICH BOARD (the re-check of 15 Nov 2026, findings 1 and 4): a Bash command that names a board's
narrative.json by an absolute path under the home's boards/ is bracketed on THAT board (and recorded
only if the session's project owns it); otherwise the board is the one of the folder the session was
started in (`CLAUDE_PROJECT_DIR`, which does not follow a `cd`; `cwd` only when it is missing:
KEEPER-BUILD.md section 14), not of `cwd`'s own name, because `cwd` follows a `cd` into a subfolder.

Whose write it is:
  * a subagent or an in-process teammate: its payload carries the LEAD's
    session_id (and its own agent_id): the lead is recorded;
  * a split-pane teammate: a process launched with --agent-id and
    --parent-session-id: its lead is recorded (anchor unknown); if the lead cannot
    be read, nothing: when in doubt, it claims nothing;
  * an Orca worker, or a headless `claude -p` run: nothing. A worker is not the
    session that keeps the board, and a headless run cannot act on a ruling. Their
    brackets are still opened and closed, so they still count as "another session's
    call" against everybody else.

Synchronous on purpose: it must land before the next answer. Prints nothing; every
error exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never write into the plugin directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "status-board"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
NAME = "narrative.json"
NAMED = re.compile(r"""(?:^|[\s'"=<>|;&(])((?:~|/)[^\s'"<>|;&()]*?/boards/[^/\s'"]+/narrative\.json)""")


def writer_of(ledger, payload: dict, tool: str) -> dict | None:
    """Who is recorded for a change this call made: {session_id, anchor, agent_id}, or None for nobody (a headless
    run, a worker, a teammate whose lead cannot be read, no session id)."""
    anchor = ledger.find_anchor()
    command = ledger.anchor_command(anchor) if anchor else None
    if ledger.is_headless(command):
        return None
    try:
        import orca_session

        lead = orca_session.teammate_parent()
        if lead is not None:
            return {"session_id": lead, "anchor": None, "agent_id": None} if lead else None
        if orca_session.is_worker(payload):
            return None
    except Exception:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    return {"session_id": session_id, "anchor": anchor, "agent_id": payload.get("agent_id")}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    import answer_ledger as ledger

    tool = payload.get("tool_name")
    event = payload.get("hook_event_name")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = Path(payload["cwd"]) if payload.get("cwd") else Path.cwd()

    if tool in FILE_TOOLS:
        if event == "PreToolUse":
            return 0
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(raw, str) or not raw:
            return 0
        target = Path(raw).resolve()
        if target.name != NAME or target.parent.parent != (ledger.STATE / "boards").resolve():
            return 0
        board = target.parent
        if not ledger.board_belongs(board, cwd):
            return 0  # another project's session writing a board that is not its own
        ledger.adopt_board(board, ledger.start_folder() or cwd)
        who = writer_of(ledger, payload, tool)
        if who:
            ledger.record_writer(board, who["session_id"], who["anchor"], tool or "tool", who["agent_id"])
        return 0

    if tool != "Bash":
        return 0
    command = tool_input.get("command")
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(command, str) or NAME not in command or not isinstance(tool_use_id, str) or not tool_use_id:
        return 0  # the shell test in hooks.json also passes a payload whose output merely mentions the file
    named = named_boards(ledger, command)
    if named:
        # The command names a board by its path under the home's boards/: that is the board it can change (the re-check
        # of 15 Nov 2026, finding 4), whichever project this session is in. Another project's board is not recorded on.
        boards = [b for b in named if b.is_dir() and ledger.board_belongs(b, cwd)]
    else:
        board = ledger.board_for(cwd)
        boards = [board] if board is not None and board.is_dir() else []
    for board in boards:
        bracket(ledger, board, payload, event, tool_use_id)
    return 0


def named_boards(ledger, command: str) -> list:
    """The boards whose narrative.json the command names by an absolute (or ~) path under the home's boards/."""
    out = []
    for raw in NAMED.findall(command):
        try:
            target = Path(os.path.expanduser(raw)).resolve()
        except (OSError, RuntimeError):
            continue
        if target.name == NAME and target.parent.parent == (ledger.STATE / "boards").resolve() and target.parent not in out:
            out.append(target.parent)
    return out


def bracket(ledger, board: Path, payload: dict, event: object, tool_use_id: str) -> None:
    if event == "PreToolUse":
        session_id = payload.get("session_id")
        ledger.open_bracket(board, tool_use_id, session_id if isinstance(session_id, str) else "", payload.get("agent_id"))
        return
    if event != "PostToolUse":
        return
    verdict = ledger.close_bracket(board, tool_use_id)
    if verdict == "changed-alone":
        who = writer_of(ledger, payload, "Bash")
        if who:
            ledger.record_writer(board, who["session_id"], who["anchor"], "Bash", who["agent_id"],
                                 since=ledger.bracket_start(board, tool_use_id))
    elif verdict in ("contested", "explained"):
        ledger.mark_unattributed(board, verdict)


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code)
