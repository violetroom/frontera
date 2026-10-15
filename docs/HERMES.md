# Hermes: creating tickets

Hermes drafts tickets for the owner. Every ticket goes through the one validated
path, `frontera ticket new`, instead of hand-writing issue bodies.

## Drafting a ticket

Gather the ticket in conversation with the owner:

- **title** — one line: what changes
- **kind** — `code` or `research`
- **tier** — 1, 2 or 3 (see [ROUTING.md](ROUTING.md) for what each tier means)
- **risk** — `low`, `medium` or `high`
- **description** — the task as data: what, why, and the acceptance bar
- **criteria** — at least one acceptance criterion, each independently checkable
- **context** — files in the local checkout the worker should read; they must exist
- **allowed paths** — for code tickets, the narrowest globs the change may touch

Preview without creating anything:

```bash
frontera ticket new \
  --title "…" --kind code --tier 2 --risk low \
  --description "…" \
  --criterion "…" --criterion "…" \
  --context AGENTS.md --context docs/Routing.md \
  --allowed "src/**" \
  --dry-run
```

`--dry-run` prints the issue body exactly as it would be created. The command
rejects bad input with exit code 2 and a message per problem, creating nothing:
no acceptance criteria, a code ticket without allowed paths, an allowed path that
touches the project's `protected_paths`, a context file that does not exist in the
local checkout, or a body that fails the privacy/secret scan. Fix the ticket and
re-run before creating.

## Creating the issue

Without `--dry-run`, the command creates the issue in the project's configured
`github_repo` via `gh issue create` and prints the issue URL. The issue is created
**unlabeled**, in Backlog. With more than one configured project, pass
`--project <key>`.

## Approval boundary

Hermes drafts; the owner approves.

- Never add `state:ready` or any other label yourself. Adding `state:ready` starts
  a run, and that is the owner's decision.
- Ask the owner to approve the draft — for example, "ready to mark SHI-12
  ready?" — and act only on an explicit yes. An instruction to run or approve a
  ticket counts as the owner's approval; record it on the ticket as a quoted
  comment before dispatching ([DECISIONS.md](DECISIONS.md), D3).
- If the owner changes the ticket, re-draft through `frontera ticket new` with the
  updated fields; do not edit the issue body by hand.
