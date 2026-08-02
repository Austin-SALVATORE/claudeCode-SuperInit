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

   Read it before you ask anything and before every reply. Asking again
   for a decision the owner already made reads as not listening — and it
   is, just to the wrong channel. (1 Aug 2026: a visual-direction ruling
   sat in `answers.json` for two minutes while the reply said the work
   was blocked on it; an earlier git ruling was re-asked twice for the
   same reason.)

   **Everything in `answers.json` is a deliberate ruling. Act on all of
   it, and do not ask again.** The page already applied the asymmetry
   before writing the file:

   - Hitting the small **I'D PICK THIS pill** on the recommended option
     records immediately. It is a precise target that cannot plausibly
     be struck by accident, and it means agreement with an analysis
     printed on the same screen.
   - **Everything else confirms** — including the rest of that same
     recommended row. Its label, its reason text and its padding are a
     large target, and the exemption belongs to the pill, not to the
     option. (Owner's rule, 1 Aug 2026, correcting an earlier version
     that exempted the whole row and so made the easiest thing to hit by
     mistake the only thing that recorded without asking.)
   - Cancelling or pressing Escape records nothing, so it never reaches
     you at all.
   - Keyboard activation confirms, because the pill is not separately
     focusable — the safe direction, and worth knowing rather than
     mistaking for a bug.

   **So re-asking is now the failure mode, not under-asking.** An earlier
   version of this rule told the agent to confirm a non-recommended
   answer in chat; with the gate on the page that asks the owner the same
   question twice, which reads as not having registered the first answer.
   Move what you find to `answered`, and say in your reply which tap you
   are acting on so a mistake is still correctable in one line.

   (1 Aug 2026: the owner asked for a double validation and I first built
   it as an instruction to myself. That is not a validation — it does
   nothing about a stray tap recording a ruling nobody made, which then
   happened within the hour and had to be reverted by hand. The lesson
   generalises past this board: **when the owner asks for a guard, ask
   which layer it has to live in.** A guard the agent applies protects
   against the agent; only a guard in the mechanism protects against the
   input.)
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

## Arm the answer watcher — once per session, before your first reply

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/status-board/watch-answers.py" "<absolute path to repo>"
```

Run it with the **Monitor** tool, `persistent: true`. Every line it prints
becomes a notification, so an answer reaches you seconds after the owner
taps rather than waiting for their next message. `SessionStart` already
tells you to do this; do it, and do not arm it twice.

**It prints one `WATCHER ARMED …` line immediately. If that line does not
appear, the arm failed** — a wrong slug, a board that does not exist, an
unreadable file. Do not read the silence that follows as a quiet board.
Read it as a dead watcher. The script also exits 1 with a `WATCHER ERROR`
line when it cannot read the file at startup, rather than sitting there
watching nothing.

Each answer arrives as **two lines — the question and the answer**:

```
BOARD ANSWER — Which type pairing?
  → Set the whole Chinese line in VP Display SC   [2026-08-01T15:57:34+02:00]
```

That the answer is there at all is the second bug this mechanism had. The
first version emitted the question alone, so every notification said *that*
a decision had been made without saying *what* — leaving the agent to wait
for the owner's next message to learn the content, which is precisely the
latency the watcher exists to remove. It looked like it was working:
notifications arrived, promptly, naming the right question. **A
notification you cannot act on is not a notification, it is a reminder to
go and look.** Caught by the owner reading the emitted line, not by any
check that was run on it.

### Why this exists, and the mistake it encodes

The `UserPromptSubmit` hook was already in place and working. It can only
fire when the owner submits a prompt, so a tap waited for their next
message. That was diagnosed as **structural** — as a property of the
system — and the diagnosis was wrong. It described the limit of one
instrument and concluded it was the limit of the world. A background
command re-invokes the agent when it exits, and a monitor delivers each
stdout line while the agent is idle. Both existed the whole time.

The tell is what makes it worth writing down: **every check run on the hook
passed.** It was registered, the plugin hook count rose, it fired on the
next message. Not one of those checks could have detected the actual
problem, and their agreement read as confirmation. That is exactly the
`verification.md` rule — *a clean result from an instrument that cannot see
the subject is not evidence, and re-running it does not make it evidence;
change instrument, not effort* — failed by the person who wrote it.

**Keep both mechanisms.** They fail differently: the hook is durable
(plugin config, every session) and slow; the watcher is fast and dies with
the session that armed it. Neither is redundant while the other can be
dead. If both report the same answer, deduplicate against what is already
in `narrative.json` — ground truth — rather than against a flag file.

And be honest about the tier: `board-answers.py` is a *mechanism*, it runs
whether or not you remember anything. The watcher is one step weaker,
because arming a monitor needs a tool call and a hook cannot make one. The
`SessionStart` hook makes the instruction arrive reliably; it cannot make
you follow it.

## The two halves — this is the whole design

|  | Who writes it | Can it go stale? |
|---|---|---|
| **Machine facts** — branch, upstream SHA, unpushed commits, working tree, who moved | The server, from `git` and transcript mtimes, on **every request** | No. Nothing remembers them. |
| **Judgement** — headline, decisions, findings, what is running | You, in `narrative.json` | **Yes.** So the board prints its own age. |

**Who moved includes you.** The lead's own row sits at the top, derived
from its session transcript's mtime exactly like every teammate's. Read
it against the `written N ago` stamp: *"team-lead just now · written 25
min ago"* is the board telling you it is describing a world you have
since moved past.

That row exists because every other staleness signal here measures
somebody else — git commits, teammate activity, elapsed minutes — while
the participant whose silence actually causes a stale narrative was the
one participant the board could not see. (Owner's observation, 1 Aug
2026, after a pipeline sat on `todo` through the commit that finished
that phase.)

Two things it must get right, both learned by getting them wrong:

- **Resolve the lead session explicitly**, from the team config's
  `leadSessionId`. A project directory holds one transcript per session,
  so "newest `.jsonl` here" can be a different session entirely and will
  cheerfully attribute someone else's activity to this board.
- **A lead transcript records the `model` of every spawn it makes.** A
  bare `"sonnet"` from dispatching the dev sits in the same file as the
  lead's own model, so accept only `claude-*` values — otherwise the
  board reports the lead as running whatever it last dispatched, which
  looks entirely plausible and is wrong.

Never type a SHA, a commit subject, a dirty-file count, or a teammate's
model into `narrative.json`. Those are derived. Typing them creates a
second source of truth that will disagree with git, and the board's whole
value is that it does not do that.

**The same trap exists *inside* the narrative, between fields you write
yourself.** `pipeline[].status` and `inFlight` encode one fact — what is
being worked on right now — in two places. Rewriting one and not the
other is the easiest mistake on this page, because you update the field
you happen to be thinking about and the other keeps asserting something
that was true an hour ago.

So treat them as a single edit: **when a phase starts, it becomes
`running` *and* appears in `inFlight`; when it is accepted, it becomes
`shipped` *and* leaves `inFlight`.** Never one without the other. Same
for `decisions` and `answered` — a decision moved out of one belongs in
the other in the same write.

(1 Aug 2026: `inFlight` correctly said the dev was building Phase 0
while `pipeline` still read `todo`, and it stayed that way through the
commit. The board displayed the new SHA a few lines above a pipeline
claiming nothing had started. The owner noticed; the page did not — it
now does, see the drift note below.)

**The board now proves this kind of staleness rather than trusting you.**
When a commit lands after the last `writtenAt`, the stamp says how many
commits arrived since these words and names the newest. Elapsed minutes
never caught it, because a narrative written two minutes ago is wrong the
instant a teammate commits. If you see that note, rewrite before replying
— it is the page telling you it is describing a repository that has moved.

Test counts are the exception: real measurements, too slow to run per
request. Cache them under `checks` **with the timestamp of the run**, and
the board shows how old they are.

## Keeping it current

The machine half needs nothing. The judgement half is on you:

- **Rewrite `narrative.json` before you reply to the owner.** Every time.
  A board that lags the conversation is worse than no board, because it
  looks authoritative.
- **And rewrite it on every event, not only before a reply.** This is the
  rule that gets missed, because the first one *feels* sufficient. It is
  not, and the reason is structural: writing the board is triggered by
  replying to the owner, and **agents start, finish and go idle precisely
  while the owner is silent.** So the field most likely to be wrong —
  `inFlight`, a present-tense claim — is refreshed on the cadence least
  correlated with its changes. Every task-notification, every teammate
  message, every spawn and every completion is a board write, *before*
  anything else you do with it. If the owner has to ask "where are we?",
  the board already failed; and if they have to ask twice, the fix is
  never "be more careful."
- **A deliverable that names a decision for the owner becomes
  `decisions`, in the same action that receives it — before any reply,
  and before the options are explained in prose.** A different failure
  from the rule above: that one is about state that goes stale
  (`inFlight`'s worked example is a claim that was true and stopped
  being true); this is about a question that has never been on the board
  at all, because it did not exist until the deliverable that named it
  arrived. A section literally titled "What the owner is being asked",
  or a list of options with a recommendation, is exactly this — route it
  onto the board the moment you receive it, not the next time you happen
  to write the file.

  **The tell is specific and does not feel like an error: you are about
  to explain the options to the owner in prose.** That sensation —
  summarising a long plan into something readable — is genuinely useful,
  which is exactly why it substitutes for the board without anyone
  noticing. Once the options have been explained well in chat, the
  pressure to also put them on the board disappears entirely, and a
  well-written paragraph is a more convincing substitute than a bad one.

  Why a chat message cannot do this job, stated rather than assumed: a
  decision that lives only in prose cannot be tapped; does not travel
  with its own question when answered, so a later reply arrives without
  the context that produced it; cannot be seen as outstanding, because
  prose scrolls away; and has no `answered` counterpart, so nobody can
  tell later whether it was decided or silently dropped. `decisions` ->
  `answered` exists precisely to make all four impossible, and none of
  it applies to a paragraph.

  Prose explaining *why* an option is recommended is still good and
  still belongs in the reply — the failure is prose that is the *only*
  place the question exists, not prose that exists at all.

  (2 Aug 2026: an architect's plan named a real decision — four options,
  a recommendation, the trade-offs — under a section titled "What the
  owner is being asked." It was written into a chat reply as prose; the
  same board write covered `findings` and `checks` only. No `decisions`
  entry. The owner: *"again you are not update the board to ask me about
  M6 decision despite the M6 design is already done and waiting for me
  to determinate."* The rule above had already fired correctly, twice,
  on both completion events — this was never a diligence gap. Nothing
  had ever told the agent that a *new* decision arriving inside a
  deliverable needed routing to the board at all.)
- **Always bump `writtenAt`.** Past 25 minutes the stamp turns amber and
  the owner can see the words are old. That is the point — never leave it
  unchanged to hide staleness.
- **Update `checks` whenever you actually run the suite**, never from
  memory or from a teammate's report you have not reproduced.
- **Clear `inFlight` in the same action that observes the work stopped**,
  not the next time you happen to write the file. An empty section is
  honest; a stale "running now" is a lie with a spinner on it.
- **Stamp every `inFlight` entry with `since`** (ISO 8601, when the work
  actually started). The page ages each entry live in the browser: past
  20 minutes with no rewrite the card turns amber and dashed, its label
  changes from "started 4 min ago" to "unconfirmed for 34 min", and the
  section heading stops saying *Running now* and says *Running now — some
  of this is unconfirmed*.

  That is deliberately not a fix for staleness; it is a fix for staleness
  being **invisible**. The board cannot see agents, so it can never know
  whether a claim is still true — but it can stop asserting something it
  has no way to check. Every other field on the page is either re-derived
  per request (cannot go stale) or dated (visibly ages). `inFlight` was
  the only present-tense claim that was neither, which is why it was the
  one that silently lied.

The page re-fetches every 3s and again the moment the tab regains focus,
so a teammate's commit or a rewritten narrative appears on its own.

## Shape of `narrative.json`

```jsonc
{
  "writtenAt": "2026-07-31T21:04:00+02:00",   // bump on every write
  "headline": "Short. What is true right now. One clause, ~60 chars.",
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
  "inFlight": [{ "who": "dev · sonnet", "what": "…", "note": "…",
                 "since": "2026-07-31T20:58:00+02:00" }],   // ages live
  "findings": [{ "sev": "high", "title": "…", "detail": "…" }],  // high|med|low
  "queued":   [{ "what": "…", "note": "…" }],
  "open":     [{ "what": "…", "who": "unassigned" }]
}
```

Backticks in any string render as inline code.

**`headline` is a display line, not a paragraph.** It sets at 27px across
the full content column, so one clause of roughly 60 characters fills a
line or two and reads as a headline. Three sentences do not become a
summary there — they become a wall of text at display size, and the owner
has to read all of it to find the one fact that changed. Put the fact in
the headline and the reasoning in `findings`, where there is room.

(1 Aug 2026: the owner reported the title "squeezed together" with space
to its right. Three causes, and fixing only one would have left it
looking wrong: `max-width: 19ch` capped it at a nineteen-character
measure; `text-wrap: balance` then *deliberately* pulled text in from the
right to even the lines out, which is exactly the ragged effect being
complained about; and the headlines being written into it were
three-sentence paragraphs. Now uncapped with `text-wrap: pretty`.)

**Mark a recommendation whenever you have one**, with the reason on the
same option. The owner asked for this explicitly. One per question — two
recommendations is not a recommendation. If you genuinely have no
preference, say so in `why` rather than marking nothing and staying
silent about it.

`recommend` is not decoration: it decides how much a bare tap
authorises. A question with no recommendation means every answer to it
needs confirming before you act, so **omitting one costs the owner a
round trip.** Withholding a recommendation you actually hold is the
expensive kind of politeness.

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

### If it is visual, it needs a picture, not a better paragraph

The rule above has a failure mode it did not originally cover. Some
findings are not *structured* — they are **visual**, and prose is not a
worse container for them, it is the wrong one. A wrapping-text defect was
once described here in three careful, measured paragraphs and read as
minor; one screenshot made it obvious in two seconds. The board could
hold the description and a filesystem path, and the reader still had to
go and open the file — which is falling back to prose wearing a filename.

So `shot` is a field on **any card** (`findings`, `inFlight`, `queued`,
`open`) and on **any decision option**:

```jsonc
"findings": [{ "sev": "high", "title": "…", "detail": "…",
               "shot": "ribbon-390.png" }],
"decisions": [{ "q": "Which treatment?", "options": [
   { "label": "A — ghost pills", "recommend": true, "why": "…",
     "shot": "candidate-a.png" }] }]
```

**`shot` is a bare filename, never a path**, and the file lives in
`~/.claude/status-board/boards/<slug>/shots/`. Copy it there; do not
reference the scratchpad. Two reasons, and both are the point rather than
a limitation:

- The server serves only that one directory, matches the name against a
  strict pattern, and re-checks containment *after* resolving — because a
  symlink dropped into `shots/` is an escape the pattern cannot see. There
  is no path to traverse because there is no path.
- Session scratchpads are deleted. A board citing one rots into broken
  images; a board owning its shots keeps them exactly as long as the words
  that cite them.

**Take the screenshot whenever you would otherwise write "open this file
and look".** If you found a defect by looking at something, the owner will
need to look at it too, and describing it better is not a substitute.

`handoffs` is the slot for content the owner must carry to someone
else — a coach, a colleague, another tool:

```jsonc
"handoffs": [{
  "to": "your coach",
  "subject": "One line naming what this is",
  "why": "Why they are being asked, and what is blocked on the answer",
  "points": [ { "q": "Short label", "detail": "The substance." } ],
  "flag": "The one point worth their attention most.",      // optional
  "since": "2026-08-02T14:00:00+02:00"                      // optional, see below
}]
```

"Copy as a message" yields **plain text with no board branding, no
project tag and no arrows** — it is going to a human who has never seen
this board, not back into a CLI. That is the opposite of the decision
copy, which is deliberately machine-tagged because it comes back to you.
Numbering is real here: the owner and the recipient will say "point 2".

**Stamp a handoff with `since` when you log it, and resolve it when the
owner tells you it is done** — the same two-part fix `inFlight` already
has, for the same reason. A handoff was the one section with no way to
become false and no way to become finished: present-tense ("you still
need to carry this"), no `since`, no ageing, no resolved state — every
other field on the page is either re-derived per request (cannot go
stale) or dated (visibly ages) or explicitly resolved (`decisions` ->
`answered`, so a question cannot silently vanish or silently persist).
`handoffs` was none of those, which is why a spent deploy checklist sat
on the board reading as an outstanding errand days after the owner had
already carried it and already ruled on the one open question inside
it.

```jsonc
"handoffs": [{
  "to": "your coach",
  "subject": "One line naming what this is",
  "points": [ { "q": "Short label" } ],
  "resolved": {
    "when": "2026-08-02T19:19:00+02:00",
    "note": "What happened — optional, defaults to \"Done\""
  }
}]
```

- **Resolve it, do not delete it.** Same argument as `answered`: a
  completed errand that just disappears from the board reads as one
  that was lost, and the owner cannot tell whether it registered. A
  resolved handoff renders as a closed card — no points, no copy
  button, nothing that reads as outstanding — but it stays visible.
- **`since` ages live in the browser**, exactly like `inFlight`'s: past
  4 hours with no resolution the card turns amber and dashed and its
  chip changes from "logged 40 min ago" to "waiting 5 h ago". 4 hours,
  not `inFlight`'s 20 minutes, because a handoff is a human errand, not
  a running process — "still on my list" is a normal state for most of
  a day, and the threshold is roughly half a working day: long enough
  that a same-day checklist has clearly had time to be carried, short
  enough to still catch one forgotten by evening.
- **Both fields are optional, and a handoff with neither is still
  correct** — it just makes no age claim and can only be cleared by
  removing it. Do not backfill `since` onto an old handoff you cannot
  actually date; a wrong guess is worse than no claim at all, same rule
  as everywhere else on this board.

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
- **Bound to 127.0.0.1 by default, and that is the right default.** It
  exposes repo paths, branch names, commit subjects and every decision on
  the board — which on a personal project can mean genuinely private
  content.

  To read it from a phone on the same network, `--host 0.0.0.0`. That
  path **requires a token**, generated once and printed at startup, and
  checked on reads *and* writes. Say plainly what it is and is not: it
  stops other devices on a shared network from wandering in, and it makes
  the exposure a deliberate act. It is **not** security — plaintext HTTP,
  token in the URL. Never on a network the owner does not control, and
  never as a substitute for asking whether the board's contents should
  leave the machine at all.
- **`--name` is display only.** The slug and port come from the repo path.
