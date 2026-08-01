#!/usr/bin/env python3
"""Tell the agent, at session start, to arm the board answer watcher.

A hook cannot call the Monitor tool — only the agent can. So this is an
instruction rather than a mechanism, and that distinction is the whole
reason the file is written this way.

The honest framing: `board-answers.py` IS a mechanism (it runs whether or
not the agent remembers anything). This hook is one step weaker — it makes
the instruction arrive reliably in every session instead of depending on the
agent recalling a line in a skill file. That is strictly better than a rule
in a document and strictly worse than something that cannot be skipped, and
it is the strongest available option given that arming a monitor requires a
tool call.

Consequence worth stating plainly: if the agent ignores this, answers still
arrive — just late, via the UserPromptSubmit hook. The two mechanisms
degrade into each other rather than failing together, which is the property
that makes keeping both worthwhile.

Prints nothing when the project has no board, so a non-board project pays
nothing. Exits 0 on every path: a reporting hook must never be able to block
the session it reports on.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE = Path(
    os.environ.get("STATUS_BOARD_HOME") or Path.home() / ".claude" / "status-board"
).expanduser()


def slug_for(repo: Path) -> str:
    # Must match serve.py, board-answers.py and watch-answers.py exactly.
    return repo.name.lower().replace(" ", "-")


def main() -> int:
    cwd = Path.cwd()
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict) and payload.get("cwd"):
            cwd = Path(payload["cwd"])
    except Exception:
        pass  # no stdin, or not JSON — cwd is a fine fallback

    if not (STATE / "boards" / slug_for(cwd) / "answers.json").is_file():
        return 0

    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    script = str(Path(root) / "status-board" / "watch-answers.py") if root else (
        "<plugin root>/status-board/watch-answers.py"
    )

    print("<board-watcher>")
    print(
        "This project has a status board with recorded answers. Arm the answer "
        "watcher now, before your first reply, using the Monitor tool with "
        "persistent: true:"
    )
    print()
    print(f'    python3 "{script}" "{cwd}"')
    print()
    print(
        "It prints one WATCHER ARMED line immediately — treat the absence of "
        "that line as a failed arm, not as a quiet board. Each later line is a "
        "new answer, delivered seconds after the owner taps instead of waiting "
        "for their next message. Do not arm it twice in one session."
    )
    print("</board-watcher>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
