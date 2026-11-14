#!/usr/bin/env python3
"""The workstream list: the shape the page is given, and what the board can
prove about it.

A workstream is one high-level subject of a project: an id, a name, a goal, a
status, where it stands, the next step, a forecast and a record. The agent
writes the list into `narrative.json` as `workstreams`; this module turns
that into the one shape the page reads, and adds the two things the agent
must not type because the board can measure them:

  * which Orca workers are on a workstream, from the id in a Task title
    (`architect | WS-03 | options for the data store` -> WS-03);
  * whether the file the list was copied from has changed since the board was
    last written. The list on the board is a copy of the project's register,
    and a copy is a second source of truth. When `source` names a file in the
    repo, the two modification times are compared, the same way the stamp
    compares the newest commit with `writtenAt`.

Wrong field names fail silently on a page that only reads the names it knows:
a row written with `nextStep` renders with no next step and nothing says why.
That mistake has a history here (SKILL.md, the `recommended` / `reason`
incident), so names this module does not read are reported back in `unread`
and the page prints them.
"""

from __future__ import annotations

import re
from pathlib import Path

ITEM_FIELDS = ("id", "name", "goal", "status", "stands", "next", "forecast", "record")
LIST_FIELDS = ("title", "source", "note", "groups", "items")
MAX_TEXT = 4000  # per field; a register row is a sentence or two, not a document
# The register is often saved a moment after the board. Inside this margin the
# two count as written together.
SOURCE_TOLERANCE_MIN = 2.0
# Splits a Task title into its parts: `role | WS-03 | subject`, or with dots.
TITLE_PARTS = re.compile(r"\s*[|·]\s*")


def _text(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value.strip()[:MAX_TEXT] if isinstance(value, str) else ""


def _is_text(value: object) -> bool:
    """A field the page can print: text, a number, or nothing at all."""
    return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))


def _fields(raw: dict, names: tuple[str, ...], unread: set[str], prefix: str = "") -> dict:
    """The named text fields of `raw`. A field of another type (a list, an
    object, true) cannot be printed; it is named in `unread` as "not text"
    instead of vanishing, which is the silent failure this module exists to
    stop."""
    unread.update(f"{prefix}{n} (not text)" for n in names if not _is_text(raw.get(n)))
    return {n: _text(raw.get(n)) for n in names}


def _details(value: object, unread: set[str]) -> list[dict]:
    """Extra labelled lines for columns a register has beyond the standard ones."""
    if value is None:
        return []
    if not isinstance(value, list):
        unread.add("details (not a list)")
        return []
    rows = [
        {"label": _text(d.get("label")), "text": _text(d.get("text"))}
        for d in value
        if isinstance(d, dict) and _is_text(d.get("label")) and _is_text(d.get("text"))
    ]
    kept = [r for r in rows if r["label"] and r["text"]]
    if len(kept) != len(value):
        unread.add("details (each needs a text `label` and `text`)")
    return kept


def _item(raw: dict, unread: set[str]) -> dict | None:
    found: set[str] = set()
    item = _fields(raw, ITEM_FIELDS, found)
    if not item["id"] and not item["name"]:
        return None  # counted by the caller; its fields are not worth naming
    unread.update(found)
    unread.update(k for k in raw if k not in ITEM_FIELDS and k != "details")
    details = _details(raw.get("details"), unread)
    return {**item, "details": details} if details else item


def _group(raw: object, unread: set[str]) -> tuple[dict | None, int]:
    """The group, and how many of its rows could not be read at all. None for
    a group that is not an object or whose `items` is not a list."""
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return None, 0
    items = [_item(i, unread) if isinstance(i, dict) else None for i in raw["items"]]
    group = {**_fields(raw, ("name", "note"), unread, "group "), "items": [i for i in items if i]}
    return group, len(items) - len(group["items"])


def normalise(raw: object) -> dict | None:
    """The page's shape, or None when the narrative has no `workstreams`.

    A list that is present and holds no readable row comes back with `error`
    set, so the page can say so instead of showing nothing.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        raw = {"items": raw}
    if not isinstance(raw, dict):
        return {"title": "", "source": "", "note": "", "groups": [],
                "error": "`workstreams` must be an object with `groups`."}
    unread: set[str] = {k for k in raw if k not in LIST_FIELDS}
    found = raw.get("groups")
    if found is not None and not isinstance(found, list):
        unread.add("groups (not a list)")
    if not isinstance(found, list):
        found = [{"items": raw.get("items")}] if isinstance(raw.get("items"), list) else []
    read = [_group(g, unread) for g in found]
    groups = [g for g, _ in read if g is not None and g["items"]]
    skipped = sum(n for _, n in read)
    lost_groups = sum(1 for g, _ in read if g is None)
    out = {**_fields(raw, ("title", "source", "note"), unread), "groups": groups}
    if not groups:
        out["error"] = (
            "`workstreams` holds no row the page can read. Each row needs an "
            "`id` or a `name`, inside `groups[].items`."
        )
    if unread:
        out["unread"] = sorted(unread)
    if skipped:
        out["skipped"] = skipped  # rows with neither an id nor a name
    if lost_groups:
        out["skippedGroups"] = lost_groups  # not an object, or `items` is not a list
    return out


def ids_of(workstreams: dict | None) -> list[str]:
    if not workstreams:
        return []
    return [i["id"] for g in workstreams["groups"] for i in g["items"] if i["id"]]


def ids_in_title(title: str, ids: list[str]) -> list[str]:
    """Workstream ids a Task title carries.

    A whole part of the title first (`role | WS-03 | subject`); failing that,
    the id as a whole word anywhere in it. Never a substring: WS-1 must not
    claim a task about WS-10.
    """
    if not title or not ids:
        return []
    parts = {p.casefold() for p in TITLE_PARTS.split(title.strip()) if p}
    exact = [i for i in ids if i.casefold() in parts]
    if exact:
        return exact
    return [
        i
        for i in ids
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", title, re.IGNORECASE)
    ]


def link(rows: list[dict], ids: list[str]) -> list[dict]:
    """The same rows, each with `ws`: the workstream ids its title carries."""
    out = []
    for row in rows:
        found = ids_in_title(row.get("title") or "", ids)
        out.append({**row, "ws": found} if found else row)
    return out


def source_check(repo: Path, source: str, narrative: Path) -> dict | None:
    """How far the register file is ahead of the board, in minutes.

    None when there is nothing to compare: no `source`, a source that is not
    a file inside the repo, or a file that cannot be read. Silence, not a
    warning: a wrong claim of drift would teach the reader to ignore a real
    one.
    """
    if not source or "://" in source:
        return None
    try:
        root = repo.resolve()
        target = (root / source).resolve()
        target.relative_to(root)  # a path that leaves the repo is not read
        if not target.is_file():
            return None
        ahead = (target.stat().st_mtime - narrative.stat().st_mtime) / 60
    except (OSError, ValueError):
        return None
    return {"aheadMin": round(ahead, 1) if ahead > SOURCE_TOLERANCE_MIN else 0}
