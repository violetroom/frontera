# frontera

A dispatcher that routes GitHub issues to coding agents running in GitHub Actions, by task kind and difficulty tier. Qwen processes ordinary tickets automatically. Claude takes promoted tickets and reviews when you ask for it. You talk to a Hermes manager agent, which drives the `frontera` CLI.

- [Plan](docs/PLAN.md): architecture, gates, build sequence
- [Routing](docs/ROUTING.md): kinds, tiers, lanes, endpoints, launch policies, promotion
- [GitHub design](docs/GITHUB.md): labels, commands, the run lifecycle, setting up a repo
- [Decisions](docs/DECISIONS.md)

## Status

The GitHub version is built and unit-tested (89 tests). It covers the issue tracker, owner-only triggers, routing, runs with promotion, independent verification, draft PRs, and agent review. The Qwen/OpenCode adapter has run for real against Tellico. The Claude adapter and the first end-to-end Actions run are next.

## Use

```bash
uv sync
cp config.example.yaml ~/.config/frontera/config.yaml   # then edit: repo paths, tracker path, private_terms
uv run frontera config check
uv run frontera next
uv run frontera run SHI-1 --dry-run              # local tracker; GitHub runs happen in Actions
uv run frontera run SHI-2 --dry-run --owner-command "do the promoted tickets"
uv run frontera review SHI-5 --dry-run --owner-command "review the finished tickets"
uv run pytest
```

`examples/tickets/` holds sample tickets in the local tracker format. Point `tracker.path` at it to try the commands.

Exit codes: `0` would run, `2` blocked (the reasons are printed), `1` config or tracker error.
