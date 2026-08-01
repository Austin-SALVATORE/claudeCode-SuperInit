# Release choreography

Sequence work so the deployed tree is never internally inconsistent.

<!-- State the deploy trigger here once it exists: which branch deploys,
     to where, and whether a push is a deploy. Everything below assumes
     a push can reach users. -->

- **Generated data and the code that reads it ship in the same commit.**
  A resolver reading a new schema while the old generated file is
  committed is a production outage with a green test suite.
- **A change and its reconciliation commit travel in one push.** No
  deploy between them.
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
