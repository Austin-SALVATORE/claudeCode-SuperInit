# Verification

Claims are verified by running commands, not by reading reports.

## Project commands

<!-- FILL THIS IN when the stack lands. Until then, every command below
     is unknown, and "I ran the tests" is an unverifiable claim. -->

| Purpose | Command | Notes |
|---|---|---|
| Install | _TBD_ | |
| Test (all) | _TBD_ | |
| Test (single file) | _TBD_ | |
| Lint | _TBD_ | |
| Typecheck | _TBD_ | delete if the language has none |
| Build | _TBD_ | delete if nothing is built |
| Run locally | _TBD_ | dev server, CLI entrypoint, REPL — whatever applies |

Delete rows that do not exist here rather than leaving them `_TBD_`
forever; a permanent `_TBD_` stops being a gap and becomes furniture.

**Record known-broken commands here too**, next to the working one.
A command that exits 0 without checking anything is the most expensive
kind of tooling defect, because it is indistinguishable from success —
and the whole team will keep reaching for it until it is written down.

## Rules

- Verify against the **committed tree**, not the working tree. A claim
  about what shipped is a claim about `origin/<default-branch>`.
- Before accepting "green", run the suite yourself and compare counts.
  Before accepting "deployed", check the artifact or the device.
- Report failures with their output. A skipped step is stated as
  skipped. "Green except known failures" is not green — name them.

- **A clean result from an instrument that cannot see the subject is
  not evidence — and re-running it does not make it evidence.** The
  trap is not the wrong answer, it is what happens next: the natural
  response to doubt is to run the same instrument harder — a wider
  regex, a second `git status`, another look at the console — and the
  agreement reads as confirmation while the blind spot is untouched.

  So the question is never "did the check pass?" but **"could this
  check have failed for the reason I care about?"** If you cannot say
  how it would fail, you have not measured anything. Change
  *instrument*, not *effort*: a different tool, a different layer, or a
  deliberate break.

- **A guard is not evidence until it has been made to fail on purpose.**
  A test written for a bug and never seen red is indistinguishable from
  a test that checks nothing, and it is worse than no test because it
  stops anyone else from looking. Break the thing it guards, watch it
  go red, name the line in the output, restore. Where a wrong "all
  clear" would be silent, the negative control belongs to a *different*
  role than the author — whoever wrote it already believes it works.

- **Ignored and generated files are invisible to git, so git cannot
  answer questions about them.** Anything gitignored can change
  completely while `git status` stays clean, and a clean status is then
  not evidence that content is unchanged. Claims about ignored trees
  come from mtimes, content hashes, or a manifest — never from git.

- **A generated artifact older than its source is stale, and nothing
  downstream will notice.** The file exists, the source now reads
  correctly, and the artifact contradicts it. Compare mtimes before
  consuming any generated tree:

  ```sh
  # `stat` is not portable: -f %m is BSD/macOS, -c %Y is GNU/Linux.
  mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1"; }
  [ "$(mtime out)" -lt "$(mtime src)" ] && echo STALE
  ```

  Or, with no `stat` at all: `[ src -nt out ] && echo STALE`.

- **A citation is a claim.** A comment, docblock, or memory note saying
  "documented in X" is not evidence that X says it — open X.
  Cross-references rot faster than the code around them, and a stale
  pointer propagates a false belief into every reader who follows it
  without looking.

- **A plan invalidates its own citations as it is executed**, and its
  first phase is the worst offender, because everything downstream is
  still unwritten when it lands. Seen in the wild: a phase whose entire
  purpose was correcting stale claims *in the code* shifted one file by
  +4 lines and another by +1 — silently making the **plan's** line
  numbers wrong for every later phase. The fix and the failure were the
  same shape, one level up.

  So **resolve a citation by content, not by number**: find the rule,
  symbol or comment it names, and treat the number as a hint. Anchor to
  a name when writing one — a citation that cannot go stale beats one
  that is merely correct today.

  And distinguish the two failure modes when reporting them, because
  they mean different things: a citation landing a few lines off is
  **drift**; a citation naming something that does not exist is a
  **defect**, and only the second means the author was wrong about the
  code.

  Do not answer this by re-numbering a long plan by hand. That acquires
  new errors faster than it sheds old ones — state the known offsets in
  the plan's header instead, and let readers resolve by name.

- **A version number is a claim about a specific machine, so read it
  off that machine.** A registry query, a docs page, or memory tells
  you what is *published* — not what is installed, and therefore not
  what any result was produced with. Every ecosystem has both kinds of
  command and they are easy to confuse: the one that reaches the
  network answers a different question from the one that reads the
  installed artifact or the lockfile. Record the command this project
  uses in the table above, and prefer the offline one.

  This matters most in a table headed "verified" or "installed and
  run", where a registry lookup silently upgrades a measurement into a
  claim about a version nothing was ever run against — and the next
  person installs *that* version and gets different behaviour. (1 Aug
  2026: a plan's verified-baseline table listed a lint plugin at 3.0.1,
  the latest release; the prototype it was measured on had 1.7.0
  installed, two majors earlier, and the manifest's `^1.7.0` could not
  have resolved to 3.0.1. One wrong row also means the method may have
  slipped in rows nobody checked.)

- **A spawn's model and kind are checkable, so check them.** What a
  definition declares is not evidence of what a spawn got:

  ```sh
  grep -oE '"model" *: *"[^"]{1,40}"' \
    ~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl \
    | sort -u
  ```

  Never `cat` or `tail` that file — it is the full JSONL transcript and
  reading it whole overflows context. A bench role with *no* file under
  `subagents/` was not spawned as a subagent at all.

- **A guarantee you have not checked is a guarantee you do not have.**
  Read it off the mechanism — the `tools:` line, the config, the actual
  command — not off the description of the mechanism.
