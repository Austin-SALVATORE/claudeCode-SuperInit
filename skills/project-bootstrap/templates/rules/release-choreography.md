# Release choreography

Sequence work so that whatever reaches users is never internally
inconsistent.

<!-- Name this project's release trigger before relying on anything
     below: which branch is released, what the act of releasing is
     (a deploy, a package publish, a tagged binary, a merged PR, a
     container push), and whether a plain `git push` can reach users on
     its own. The answer changes how much of this file applies — a
     library that publishes on tag has a very different blast radius
     from a service that deploys on merge. -->

- **Generated data and the code that reads it ship in the same commit.**
  A resolver reading a new schema while the old generated file is
  committed is an outage with a green test suite behind it.
- **A change and its reconciliation commit travel in one push.** Nothing
  is released between them.
- **Targeted `git add` only** — never `-A` or `.`. Other teammates have
  work in the same tree, and staging theirs by accident is how
  half-finished work ships.
- Commit at phase boundaries, with messages that state **what was
  verified**, not just what changed.
- Scan for secrets before committing anything that reads credentials or
  touches config.
- Anything irreversible or outward-facing — migration, deploy, force
  push, published artifact — needs the owner's explicit approval first.
  Speed is never the reason to skip it.
