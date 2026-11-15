---
name: board-link
description: Choose whether the status board's link is also in the status line (claude-hud's or another). The button that opens the board is always above the prompt. Use when the owner runs /project-init:board-link, when a `<board-link-offer>` or `<board-link-statusline>` line asks for it, or when /project-bootstrap raises the question. Edits nothing without the owner's yes.
---

# Board link: the button, and the link in the status line

The board's **button is always above the prompt** (the plugin's mod, on by default: one button, `9: board localhost:8800/<name>`, that
opens the board in the owner's browser, with no link in it). **The status line** can carry a clickable `board` beside it, after the
context usage, which leads to the board itself and only reaches a page that asks the owner to press the button; this skill puts it there,
through a small filter, after the owner's yes. That link is an addition: it never replaces the button. The two switches of the plugin's own are
the plugin's options `board_link_row` ("Board link above the prompt", on by default) and `board_link_status_line` ("Board link in the
status line", off by default). **The button is drawn whenever either is on; only both off hides it, and the owner can do that in `/config`
(search for "board link"). This skill never turns either switch off.**

Nothing here is needed for the plugin to work: the button is the default, and claude-hud is optional. **The owner decides; you
only ask, show and do what the owner says.** Ask in prose or with `AskUserQuestion`, one question at a time.

## The commands you use

All print exactly one line of JSON. Exit code 0: done (or nothing to do), 2: refused, nothing written (the JSON has `refused`
and the reason), 1: unexpected, nothing written (the JSON has `error`).

- `python3 "${CLAUDE_PLUGIN_ROOT}/status-board/statusline_setup.py" status`: writes nothing. Keys: `has_status_line`,
  `is_claude_hud`, `wrapped` (the filter is already in), `hand_made_filter` (a hand-made board filter from before 2.3.0, a script named `hud-board-link.py` that some set-ups pipe their status line through, is in),
  `claude_hud_installed`, `recorded` (what was recorded before, or null).
- `python3 "${CLAUDE_PLUGIN_ROOT}/status-board/statusline_setup.py" add --dry-run`: writes nothing. Prints `old` and `new`, the
  status line command as it is and as it would be, or a refusal.
- `python3 "${CLAUDE_PLUGIN_ROOT}/status-board/statusline_setup.py" remove --dry-run`: the same for taking the link out.
- `python3 "${CLAUDE_PLUGIN_ROOT}/status-board/statusline_setup.py" record --answer row`: writes the answer (one of `row`,
  `status-line`, `both`, `declined`; with `--hud proposed` or `--hud declined` for claude-hud). It overwrites: the owner may
  choose again.
- The two switches, set with Claude Code's own command, values as strings:
  `printf '%s' '{"board_link_row":"true","board_link_status_line":"false"}' | claude plugin configure project-init@claudecode-superinit --values-stdin`
  (give only the switch you change; use the plugin's id as `/plugin` lists it if it is not that one). A change takes effect
  at once: Claude Code reloads the mod.

**Never edit the owner's `settings.json` yourself, never write the `statusLine` key by hand, never run `hud install`, and never
install claude-hud: the commands above are the only way anything is written.** Where the owner's permission settings ask, Claude Code's own
permission prompt shows each of them to the owner.

## 1. Where things stand

Run `status`. Say in one or two sentences what you found (a status line or none; claude-hud's or another; the filter already
there). If `wrapped` is true and the owner only wants to change the switches, skip to 3. If `recorded` is not null, say what was
chosen before and ask again anyway: this skill is asked for, not imposed.

## 2. The question

One `AskUserQuestion`, header "Board link": **"Should the board's link also be in the status line?"** Two answers; neither turns the button off.

- **Above the prompt only (as now).** "One button above the prompt, 9: board localhost:8800/<name>, that opens the board; press it, or type 9 on an empty prompt. Nothing else to install."
- **Also in the status line.** "The same button, and a clickable 'board' in your status line, beside the context bar. It needs a status line: claude-hud (another author's plugin) or your own. The button stays: the link is an addition, and it only reaches a page that asks you to press the button."

## 3. If the answer is "Above the prompt only"

Set the switches (`board_link_row` true, `board_link_status_line` false). If `wrapped` is true, offer to take the link out of the
status line: the "Take the link out of the status line" step below. Then `record --answer row`. Say what is now true, in one
sentence.

## 4. If the answer is "Also in the status line"

Look at what `status` said:

- **No status line** (`has_status_line` false) **and claude-hud not installed** (`claude_hud_installed` false): this plugin does
  not create a status line. Propose claude-hud, in these words, and leave it to the owner:

  > claude-hud is a status line plugin by Jarrod Watts, not part of project-init; it shows the model, the context bar, git and more, and the board's link can sit in it. To install it, type these yourself: `/plugin marketplace add jarrodwatts/claude-hud`, then `/plugin install claude-hud`, then `/reload-plugins`, then `/hud` (it asks before it sets your status line). Then run `/project-init:board-link` again. Until then the row above the prompt stays.

  Offer three answers: "I'll install it" (`record --answer row --hud proposed`), "Not now" (record nothing), and
  "Don't suggest claude-hud again" (`record --answer row --hud declined`). You do not install it, you do not run
  `hud install`, you do not set the owner's status line: the owner types the commands. Stop here: the row stays.
- **claude-hud installed but not the owner's status line** (`claude_hud_installed` true, `is_claude_hud` false, no status line): tell
  the owner to run `/hud` first (it asks before it sets the status line), then run this again. The row stays until then. Stop.
- **A status line** (`has_status_line` true, claude-hud's or another): offer the filter. Run `add --dry-run`; but if `hand_made_filter` is true, run `add --replace-hand-made --dry-run` instead, as in the first bullet below.
  - If `hand_made_filter` is true, the command pipes through a hand-made board filter from before 2.3.0, a script named `hud-board-link.py` (a plain `add` refuses it, with the
    code `hand-made`). Offer to replace it with the plugin's (same place, same words; that script stays where it is). Run
    `add --replace-hand-made --dry-run`, then **show the old and the new command** exactly as it printed them, in code blocks, say
    in one sentence that only the filter is replaced, and ask: "Make this edit?" Yes or no. Without the owner's yes, leave the
    command alone and say that the button above the prompt stays. **Do not run the edit itself before the owner's yes.**
  - If a dry run says `refused` for any other reason (the `hand-made` refusal is the case above), give the reason in one
    sentence, say "The row above the prompt stays.", set the switches for the row only, `record --answer row`, and stop. Nothing
    was written.
  - Otherwise **show the old and the new command** exactly as `add --dry-run` printed them, in code blocks, say in two
    sentences what the new one does (it runs the owner's command as before, then adds the link after the context usage; any failure
    prints the status line exactly as it came; a backup of `settings.json` is kept), and ask: "Make this edit?" Yes or no.
    **Do not run `add` before the owner's yes.**

The button above the prompt stays on in every case: the status line's link is an addition, and this skill never switches the row off.

## After the owner's yes

Only now. Run `python3 "${CLAUDE_PLUGIN_ROOT}/status-board/statusline_setup.py" add` (with `--replace-hand-made` after it only if the owner agreed to that), and read its JSON:

- exit 0 and `written` true: the edit is made. Set `board_link_status_line` true (and leave `board_link_row` on). `record --answer both`.
- exit 0 and `already` true: it was there; set the switches the same way.
- exit 2: it was refused after all (it says why); nothing was written. Say "The row above the prompt stays." and `record --answer row`.
- exit 1: say that it failed and nothing was written. Do not try again with other flags.

## 5. Has the link been seen?

Tell the owner the link appears at the next refresh of the status line, a few seconds, right after the context usage. Ask: "Do you see
`board` in your status line now?"

- Yes: nothing more. Do not offer to turn the button above the prompt off; the status line's link is an addition, and the button
  stays whatever the status line shows (claude-hud's set-up may rewrite the command, claude-hud may be removed, Python may be missing).
- No: do not switch anything off. Say that the button stays, that a status line refreshes every few seconds or on the next
  message, and that `/project-init:board-link` can take the link out again.

## Take the link out of the status line

When the owner asks (it is the "Take the link out of the status line" choice): run `remove --dry-run`, show the old and the new command,
and ask. After the owner's yes run `python3 "${CLAUDE_PLUGIN_ROOT}/status-board/statusline_setup.py" remove` (exit 0, `written` true, or `already`), then set `board_link_row` true and
`board_link_status_line` false, and `record --answer row`.

## Rules

- Each of the owner's yeses covers one edit. A refusal is not an error to work around: say the reason and stop. The row above
  the prompt always stays where the status line cannot be edited.
- The question is raised by the plugin at most once per user (one line at the first interactive session, or by
  `/project-bootstrap`). Running this skill is always the owner's own act; a "no" or no answer changes nothing, and nothing is
  raised again.
- Say "the owner" in notes you write down, never the owner's name. Nothing of the owner's machine (a home path) goes into what you write.
