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

Happy-path verification is insufficient. The pass has to include the
boundaries and the entry paths nobody demos: first and last element,
empty and oversized input, every route into the feature rather than the
one you built it through, interrupted and repeated operations, and
whatever the system prints when it is unhappy.

Write the concrete list for *this* kind of project — the categories
above are the shape, not the checklist:

- **UI** — keyboard focus, direct URLs, back navigation, small
  viewports, console errors.
- **CLI** — piped vs TTY output, non-zero exit codes, `--help`,
  interrupted runs, unusual argument order.
- **Library / API** — the documented examples run verbatim, error
  types are what the docs claim, semver impact of every signature
  change.
- **Data / batch** — empty input, duplicate keys, re-running the same
  job twice, partial failure halfway through.

Always add the milestone-specific checks: the behaviors this milestone
introduced, **and the behaviors it changed**.

## 4. Close

Only after the owner's green light: mark the milestone shipped in the
roadmap with its date and an honest summary of what shipped
(**including what was cut**), record durable decisions in memory, and
empty the milestone's backlog into either the next milestone or an
explicit deferred list. Nothing stays "probably fine".
