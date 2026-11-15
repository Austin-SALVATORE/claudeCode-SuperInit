#!/usr/bin/env python3
"""hooks/hooks.json holds only the keys Claude Code's documentation names (release 2.3.0, 15 Nov 2026).

    python3 tests/test_hooks_json.py --plugin <plugin dir>

At the start of a session Claude Code 2.1.325 told the owner: `project-init: hooks.json: unknown keys "description" in
hooks.UserPromptSubmit[0], "description" in hooks.UserPromptSubmit[1], ... and 5 more ignored`. The file had a `description` in each of
its ten hook groups. The documentation names a place for a description only once, at the top of the file, as one string; it names none
for a hook group or a hook. What the ten texts say now stands in hooks/README.md, one section per group, word for word.

What is tested: that every key of hooks.json, at every level, is one the documentation names (the lists below are data, each with
the address and the sentence it comes from); that the events, matchers, commands, timeouts and flags are exactly those of the file
before the change (its structure, without the descriptions, as a hash); that the `modules` entry is unchanged; that each of the ten
texts is in hooks/README.md unchanged (by hash) under the heading of its group, in the order of the file; and that the README says
why they are there.

Run it against the file before the change and the key test, the structure test and the README tests fail. Nothing here starts a
process or binds a port.
Exit code: 0 when every test passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--plugin", required=True, help="checkout of the plugin to test")
PLUGIN = Path(ap.parse_args().plugin).resolve()
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
README = PLUGIN / "hooks" / "README.md"
sys.dont_write_bytecode = True

# The keys the documentation names, by level, each with where (read on 15 Nov 2026, Claude Code 2.1.325).
DOCUMENTED = {
    "the file": {
        "hooks": 'https://code.claude.com/docs/en/plugins/components.md, Hooks: "Save the plugin\'s hooks in `hooks/hooks.json` at the plugin root, '
                 'under a top-level `"hooks"` key, in the same shape as the `hooks` object in `settings.json`."',
        "modules": 'https://code.claude.com/docs/en/plugins/mods/reference.md, Files: "`modules`: an array with one path, relative to this file, to the '
                   'hooks module ... Can also hold settings hooks under `hooks`."',
        "description": 'https://code.claude.com/docs/en/hooks.md, Reference scripts by path, Plugin scripts: "Define plugin hooks in `hooks/hooks.json` '
                       'with an optional top-level `description` field."',
    },
    "a hook group": {
        "matcher": 'https://code.claude.com/docs/en/hooks.md, Matcher patterns: "The `matcher` field filters when hooks fire."',
        "hooks": 'https://code.claude.com/docs/en/hooks.md, Configuration: "Define one or more hook handlers to run when matched" (the inner `hooks` array).',
    },
    "a hook": {
        "type": 'https://code.claude.com/docs/en/hooks.md, Common fields: "`type` | yes | `\\"command\\"`, `\\"http\\"`, ..."',
        "command": 'https://code.claude.com/docs/en/hooks.md, Command hook fields: "`command` | yes | Shell command to execute."',
        "args": 'https://code.claude.com/docs/en/hooks.md, Command hook fields: "`args` | no | Argument list."',
        "timeout": 'https://code.claude.com/docs/en/hooks.md, Common fields: "`timeout` | no | Seconds before canceling."',
        "async": 'https://code.claude.com/docs/en/hooks.md, Command hook fields: "`async` | no | If `true`, runs in the background without blocking."',
        "asyncRewake": 'https://code.claude.com/docs/en/hooks.md, Command hook fields: "`asyncRewake` | no | If `true`, runs in the background and wakes Claude on exit code 2."',
        "if": 'https://code.claude.com/docs/en/hooks.md, Common fields: "`if` | no | Permission rule syntax to filter when this hook runs."',
        "shell": 'https://code.claude.com/docs/en/hooks.md, Command hook fields: "`shell` | no | Shell to use for this hook."',
        "statusMessage": 'https://code.claude.com/docs/en/hooks.md, Common fields: "`statusMessage` | no | Custom spinner message displayed while the hook runs."',
        "once": 'https://code.claude.com/docs/en/hooks.md, Common fields: "`once` | no | If `true`, Claude Code removes the hook after its first successful run."',
        "onFailure": 'https://code.claude.com/docs/en/hooks.md, Command hook fields: "`onFailure` | no | Whether the action that triggered the hook goes ahead when the hook fails."',
    },
}
# The file's structure before the change, without the ten descriptions and without `$schema` (a key the documentation names for a
# settings file and for plugin.json, never for hooks.json): the sha1 of its canonical JSON (sorted keys, no spaces), taken from the
# hooks.json of the release before this one with part A's and part B's patches applied (the copy of 15 Nov 2026, 2.3.0 in the making).
# A hook that is changed on purpose changes this hash: say so in the commit that does it.
STRUCTURE_SHA1 = "c6a532a9f362878fd17becb3c40eac5c936bf934"
# The sha1 of each of the ten texts as they were in hooks.json, by (event, index of the group in the event's list).
DESCRIPTION_SHA1 = {
    ("UserPromptSubmit", 0): "d6ea7c879a84",
    ("UserPromptSubmit", 1): "75c25f7a6d44",
    ("SessionStart", 0): "44e422aa5cfc",
    ("SessionStart", 1): "68d800939325",
    ("SessionStart", 2): "658c3871652b",
    ("Stop", 0): "4fae8e032719",
    ("PostToolUse", 0): "8fdd6d3e28c1",
    ("PostToolUse", 1): "252f3109d264",
    ("PostToolUse", 2): "40faafa3a791",
    ("PreToolUse", 0): "1b72406e58a6",
}


def load() -> dict:
    return json.loads(HOOKS_JSON.read_text())


def test_every_key_at_every_level_is_one_the_documentation_names():
    data = load()
    problems = []
    for key in data:
        if key not in DOCUMENTED["the file"]:
            problems.append(f"the file: {key}")
    for event, groups in data.get("hooks", {}).items():
        for number, group in enumerate(groups):
            for key in group:
                if key not in DOCUMENTED["a hook group"]:
                    problems.append(f"{event}[{number}]: {key}")
            for position, hook in enumerate(group.get("hooks", [])):
                for key in hook:
                    if key not in DOCUMENTED["a hook"]:
                        problems.append(f"{event}[{number}].hooks[{position}]: {key}")
    assert not problems, "keys the documentation does not name: " + "; ".join(problems[:12])
    for level, names in DOCUMENTED.items():
        for key, source in names.items():
            assert source.startswith("https://code.claude.com/docs/en/"), (level, key)


def test_the_events_matchers_commands_timeouts_and_flags_are_those_of_the_file_before():
    data = load()
    stripped = json.loads(json.dumps(data))
    stripped.pop("$schema", None)
    for groups in stripped.get("hooks", {}).values():
        for group in groups:
            group.pop("description", None)
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"))
    found = hashlib.sha1(canonical.encode()).hexdigest()
    assert found == STRUCTURE_SHA1, f"the structure differs from the file before (sha1 {found}): {canonical[:300]}"
    assert json.dumps(data, sort_keys=True, separators=(",", ":")) == canonical, "the file holds a key beside the structure: it is not only that structure"


def test_the_modules_entry_is_unchanged():
    assert load().get("modules") == ["./board-link.tsx"], load().get("modules")


def sections() -> list[tuple[str, str, str]]:
    """The README's sections: (heading, the lines before the text, the text): the text is the section's last paragraph."""
    text = README.read_text()
    out = []
    for block in re.split(r"(?m)^## ", text)[1:]:
        heading, _, body = block.partition("\n")
        paragraphs = [p for p in body.strip("\n").split("\n\n") if p.strip()]
        out.append((heading.strip(), "\n\n".join(paragraphs[:-1]), paragraphs[-1] if paragraphs else ""))
    return out


def test_each_of_the_ten_texts_is_in_the_hooks_readme_unchanged_under_its_group_in_the_order_of_the_file():
    assert README.is_file(), "hooks/README.md does not exist"
    found = sections()
    expected = [(event, number) for event, groups in load()["hooks"].items() for number in range(len(groups))]
    assert [name for name in DESCRIPTION_SHA1] == expected, "the file's groups are not the ten the texts belong to"
    assert [h.split()[0] for h, _, _ in found] == [f"{event}[{number}]" for event, number in expected], \
        f"the README's sections are not the file's groups in order: {[h for h, _, _ in found]}"
    for (event, number), (_, _, text) in zip(expected, found):
        assert hashlib.sha1(text.encode()).hexdigest()[:12] == DESCRIPTION_SHA1[(event, number)], \
            f"the text of {event}[{number}] is not the one that was in hooks.json: {text[:120]}"


def test_each_section_names_the_command_it_is_about():
    data = load()["hooks"]
    for (heading, before, _text) in sections():
        event, number = re.match(r"(\w+)\[(\d+)\]", heading).groups()
        group = data[event][int(number)]
        for hook in group["hooks"]:
            script = re.search(r"hooks/([\w.-]+\.py)", hook["command"])
            name = script.group(1) if script else hook["command"][:40]
            assert name in before, f"{heading} does not name {name}: {before[:200]}"
        if group.get("matcher"):
            assert group["matcher"] in before, f"{heading} does not say its matcher {group['matcher']}"


def test_the_readme_says_why_the_texts_are_here_and_not_in_the_file():
    text = " ".join(README.read_text().split())
    assert "unknown keys" in text and "description" in text, "the README does not say what Claude Code reported"
    assert "documentation" in text and "one string" in text, "the README does not say that the documentation names one place, one string"
    assert "word for word" in text or "unchanged" in text


def test_no_hook_group_or_hook_carries_a_description_any_more():
    data = load()
    named = [f"{event}[{n}]" for event, groups in data["hooks"].items() for n, group in enumerate(groups)
             if "description" in group or any("description" in hook for hook in group["hooks"])]
    assert not named, f"descriptions still in hooks.json: {named}"


def main() -> int:
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001 - a test runner reports every failure
            failed += 1
            reason = " ".join(str(exc).split())[:300] or type(exc).__name__
            print(f"FAIL  {name}: {type(exc).__name__}: {reason}")
    print(f"{len(tests) - failed} passed, {failed} failed, plugin under test: {PLUGIN}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
