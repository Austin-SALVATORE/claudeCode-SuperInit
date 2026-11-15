#!/usr/bin/env python3
"""The status line's way to the plugin's filter (release 2.3.0). Copied by `statusline_setup.py add` into the plugin's data
folder as `statusline-board-link.py`, so that the status line command never names a versioned plugin folder.

It finds the installed plugin from Claude Code's own record (`<config>/plugins/installed_plugins.json`), runs that version's
`status-board/statusline_filter.py` on the lines it was given, and prints what comes back. Its own folder is the plugin's data
folder (a status line command is not given CLAUDE_PLUGIN_DATA), and the filter is told so. On any error, or when the plugin
is not installed, it prints the lines exactly as they came: the status line never breaks because of this.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_KEY = "project-init@claudecode-superinit"


def main() -> None:
    data = sys.stdin.buffer.read()
    out = data
    try:
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        record = json.loads((config / "plugins" / "installed_plugins.json").read_text())
        for entry in record.get("plugins", {}).get(PLUGIN_KEY, []):
            filter_py = Path(str(entry.get("installPath", ""))) / "status-board" / "statusline_filter.py"
            if filter_py.is_file():
                env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(Path(__file__).resolve().parent)}
                done = subprocess.run([sys.executable, "-I", "-S", "-B", str(filter_py)], input=data, capture_output=True, timeout=5, env=env)
                if done.returncode == 0 and done.stdout:
                    out = done.stdout
                break
    except Exception:  # noqa: BLE001 - any failure prints the status line as it came
        out = data
    sys.stdout.buffer.write(out)


if __name__ == "__main__":
    main()
