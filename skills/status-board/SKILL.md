---
name: status-board
description: Report to the owner on a live local web page instead of prose. Use before EVERY substantive reply to the owner — status, findings, decisions, questions for them, anything they must carry elsewhere, or any reply containing a structured list. Works for any project, not one specific repo.
---

# Status board

## Before every reply — run this

1. **Is anything in my reply structured?** A numbered list, a set of
   questions, a comparison, a spec, findings, options. → It goes on the
   board. The reply points at it.
2. **Does the narrative still match what is true?** → Rewrite it and
   bump `writtenAt`. Every time, not when it feels stale.
3. **Read `answers.json` — do not wait to be told.** The page records
   every tap server-side the instant it happens, and **nothing notifies
   you**. Tapping also copies the answer to the owner's clipboard, but
   pasting it is an extra step they will not always take, and from your
   side an answered question is indistinguishable from an ignored one.

   ```bash
   cat ~/.claude/status-board/boards/<slug>/answers.json
   ```

   Read it before you ask anything and before every reply. Then move
   what you find to `answered` with what they chose. Asking again for a
   decision the owner already made reads as not listening — and it is,
   just to the wrong channel. (1 Aug 2026: a visual-direction ruling sat
   in `answers.json` for two minutes while the reply said the work was
   blocked on it; an earlier git ruling was re-asked twice for the same
   reason.)
4. **Is there no slot for what I am holding?** → **Add the field.** A
   missing slot is a gap in the model, never a licence to fall back to
   prose.

The reply that remains should be short, and should be the things prose
is genuinely better at: what I judge, what I got wrong, what I could not
verify. Everything enumerable is on the page.

The owner reads prose slowly and loses the thread in it. They said so
directly. So status goes on a page they can glance at, and the reply in
chat stays short.

**The board is the report. The chat message is the pointer to it.**

## Start it

One engine, one board per project. From anywhere:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/status-board/serve.py" --repo <absolute path to repo> --name "<Display Name>" &
```

It prints the URL. The port is derived from the repo path with crc32, so a
project always lands on the same URL and two projects never collide. Check
before starting a second one:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:<port>/report.json
```

`200` means it is already running — do not start another. `--port N`
overrides if something else holds the port.

**Backgrounded, the URL it prints will not reach you — so derive the port
from the server's own PID, never by process name:**

```bash
lsof -a -iTCP -sTCP:LISTEN -P -n -p <pid>
```

`lsof -c python3` does **not** match it: a Homebrew framework build reports
as `Python`, so a name-matched lookup silently returns some *other*
process's port. That is a wrong answer that looks exactly like a right one
— it happened on 1 Aug 2026 and an unrelated proxy's port was published as
the board URL. Whatever port you end up with, `curl` it before quoting it;
a port nobody verified is a guess.

Per-project state lives in `~/.claude/status-board/boards/<slug>/narrative.json`,
created on first run. It stays in your home directory on purpose — this
plugin's own directory is replaced wholesale on every update, and boards
written there would be destroyed by an upgrade. `STATUS_BOARD_HOME`
overrides the location.

## The two halves — this is the whole design

|  | Who writes it | Can it go stale? |
|---|---|---|
| **Machine facts** — branch, upstream SHA, unpushed commits, working tree, who moved | The server, from `git` and transcript mtimes, on **every request** | No. Nothing remembers them. |
| **Judgement** — headline, decisions, findings, what is running | You, in `narrative.json` | **Yes.** So the board prints its own age. |

Never type a SHA, a commit subject, a dirty-file count, or a teammate's
model into `narrative.json`. Those are derived. Typing them creates a
second source of truth that will disagree with git, and the board's whole
value is that it does not do that.

Test counts are the exception: real measurements, too slow to run per
request. Cache them under `checks` **with the timestamp of the run**, and
the board shows how old they are.

## Keeping it current

The machine half needs nothing. The judgement half is on you:

- **Rewrite `narrative.json` before you reply to the owner.** Every time.
  A board that lags the conversation is worse than no board, because it
  looks authoritative.
- **Always bump `writtenAt`.** Past 25 minutes the stamp turns amber and
  the owner can see the words are old. That is the point — never leave it
  unchanged to hide staleness.
- **Update `checks` whenever you actually run the suite**, never from
  memory or from a teammate's report you have not reproduced.
- Clear `inFlight` when work stops. An empty section is honest; a stale
  "running now" is a lie with a spinner on it.

The page re-fetches every 3s and again the moment the tab regains focus,
so a teammate's commit or a rewritten narrative appears on its own.

## Shape of `narrative.json`

```jsonc
{
  "writtenAt": "2026-07-31T21:04:00+02:00",   // bump on every write
  "headline": "Short. What is true right now.",
  "checks": { "tests": "956 / 80 files", "typecheck": "clean",
              "at": "2026-07-31T20:59:30+02:00" },
  "pipeline": {                                // omit if work is not a sequence
    "title": "Workout Mode redesign",
    "steps": [ { "name": "3 · set screen", "status": "shipped" } ]
  },                                           // shipped | held | running | todo
  "decisions": [{
    "q": "The question, in the owner's vocabulary.",
    "why": "Why it is being asked now, and why it is cheap to answer now.",
    "options": [
      { "label": "What I would do",
        "recommend": true,                     // at most ONE per question
        "why": "The reason, in one line." },
      "A plain string is a fine option when there is nothing to add"
    ]
  }],
  "inFlight": [{ "who": "dev · sonnet", "what": "…", "note": "…" }],
  "findings": [{ "sev": "high", "title": "…", "detail": "…" }],  // high|med|low
  "queued":   [{ "what": "…", "note": "…" }],
  "open":     [{ "what": "…", "who": "unassigned" }]
}
```

Backticks in any string render as inline code.

**Mark a recommendation whenever you have one**, with the reason on the
same option. The owner asked for this explicitly. One per question — two
recommendations is not a recommendation. If you genuinely have no
preference, say so in `why` rather than marking nothing and staying
silent about it.

Tapping an option copies **the project name, the question and the chosen
answer** to the clipboard as two lines:

```
[acme-api board] Re: Confirm the retention window before I write the migration
→ Correct — 90 days, and purge the soft-deleted rows in the same pass
```

So the reader on the other end never has to reconstruct what was being
answered, or which project it came from. Consequences for how you write:

- **Options are sentences the owner would say**, not button labels —
  they arrive as the owner's message.
- **Questions must read standalone.** The question travels with the
  answer, so a `q` that only makes sense next to the card around it
  ("Which one?") arrives useless.

When a decision is answered, move it from `decisions` to `answered`
with `chose`, a `note` on what it means, and `when`. Do not just delete
it — a question that silently vanishes reads as a question that was
lost, and the owner cannot tell whether you registered the answer.

**The `answered` entry is also your only record that you saw the tap.**
Since answers arrive silently in `answers.json`, a decision still
sitting in `decisions` after the owner answered it is the visible
symptom of the failure in step 3 above — check there first when the
owner says "I already told you".

## If it is structured, it goes on the board

The test is not "is this status?" — it is **"am I about to write a
structured list in chat?"** A numbered list, a set of questions, a
comparison, a spec: all of it belongs on the board, and a pointer to it
belongs in the reply.

This fails quietly, because prose in chat still *communicates*. The
symptom is content that has no obvious slot in `narrative.json`, so it
gets typed into the reply instead — and the board silently stops being
the place where things live. **A missing slot is a gap in the model, not
a licence to fall back to prose.** Add the field.

`handoffs` is the slot for content the owner must carry to someone
else — a coach, a colleague, another tool:

```jsonc
"handoffs": [{
  "to": "your coach",
  "subject": "One line naming what this is",
  "why": "Why they are being asked, and what is blocked on the answer",
  "points": [ { "q": "Short label", "detail": "The substance." } ],
  "flag": "The one point worth their attention most."       // optional
}]
```

"Copy as a message" yields **plain text with no board branding, no
project tag and no arrows** — it is going to a human who has never seen
this board, not back into a CLI. That is the opposite of the decision
copy, which is deliberately machine-tagged because it comes back to you.
Numbering is real here: the owner and the recipient will say "point 2".

## Rules

- **Nothing here is a substitute for verification.** The board reports
  what you already checked. Putting a claim on it does not make it true,
  and a green `checks` block you did not personally reproduce is exactly
  the silent-wrong-all-clear the project's verification rules exist to
  prevent.
- **Read-only against the repo and against team infrastructure.** The
  server runs `git` in reporting modes only. Task lists and mailboxes
  under `~/.claude/tasks/` and `~/.claude/teams/` are maintained by Claude
  Code — never hand-edit them.
- **Bound to 127.0.0.1.** It exposes repo paths, branch names and commit
  subjects; do not bind it to anything else.
- **`--name` is display only.** The slug and port come from the repo path.
