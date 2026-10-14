# frontera

A dispatcher that routes tracker tickets (Jira, planned) to coding agents by task kind and difficulty tier. Qwen processes ordinary tickets automatically. Claude takes promoted tickets and reviews when you ask for it. You talk to a Hermes manager agent, which drives the `frontera` CLI.

- [Plan](docs/PLAN.md): architecture, gates, build sequence
- [Routing](docs/ROUTING.md): kinds, tiers, lanes, endpoints, launch policies, promotion
- [Decisions](docs/DECISIONS.md)

## Status

Step 2 (dry run) is done. There is config validation, lane routing, preflight checks, a secret and privacy scanner, and a context-pack and prompt builder, all running against a local JSON ticket tracker. Agents are not launched yet. The Jira adapter and real runs come in step 3.

## Use

```bash
uv sync
cp config.example.yaml ~/.config/frontera/config.yaml   # then edit: repo paths, tracker path, private_terms
uv run frontera config check
uv run frontera next
uv run frontera run SHI-1 --dry-run
uv run frontera run SHI-2 --dry-run --owner-command "do the promoted tickets"
uv run frontera review SHI-5 --dry-run --owner-command "review the finished tickets"
uv run pytest
```

`examples/tickets/` holds sample tickets in the local tracker format. Point `tracker.path` at it to try the commands.

Exit codes: `0` would run, `2` blocked (the reasons are printed), `1` config or tracker error.
