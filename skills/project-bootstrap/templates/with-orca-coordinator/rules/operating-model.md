# Operating model

How work moves in this project under Orca. The orca-coordinator plugin
runs the workers: who coordinates, who does what, how work is handed out
and checked, and the list of workstreams are its own, so this file does
not repeat them. It holds what is the project's own: the decisions.

## The coordinator

The orca-coordinator plugin decides which session coordinates and what
the coordinator does itself. This file does not.

## Decisions

The owner's go, and every choice that is the owner's, goes on the status board as
a `decisions` card with a recommendation, not into a chat message. Label
each choice with who decided it and when ("the owner, 3 Mar"; "the
coordinator"), so that nobody has to remember which it was.
