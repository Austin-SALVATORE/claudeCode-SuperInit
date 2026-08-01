---
name: milestone-acceptance
description: Run the milestone close-out — independent verification, exploratory acceptance pass, and roadmap close. Use when a teammate reports a milestone feature-complete, before calling it shipped.
---

# Milestone acceptance

A milestone is not closed by a report saying it is. It is closed by
verification, an exploratory pass, and the owner's word — in that
order.

## 1. Verify the claims (lead)

Delegate to the `claim-verifier` agent: commits exist and are pushed,
suite count matches, typecheck clean, build output as claimed,
absence-claims proven by sweep. Use the commands recorded in
`.claude/rules/verification.md`. Discrepancies go back to the teammate
before anything else happens.

## 2. Review the work against its contract

Read the spec the milestone was reviewed against and check the delivery
**item by item** where content is involved. Reports summarize; specs
are authority. Where the teammate made a judgment call the spec didn't
cover, confirm it was documented rather than silently baked in.

## 3. Exploratory acceptance pass (owner)

Happy-path verification is insufficient. Cover: keyboard focus, first
and last rows, every navigation entry path, direct URLs, browser back,
small-viewport overflow, console errors. Add the milestone-specific
checks — the behaviors this milestone introduced, **and the behaviors
it changed**.

## 4. Close

Only after the owner's green light: mark the milestone shipped in the
roadmap with its date and an honest summary of what shipped
(**including what was cut**), record durable decisions in memory, and
empty the milestone's backlog into either the next milestone or an
explicit deferred list. Nothing stays "probably fine".
