# Working on Frontera

Frontera routes GitHub issues to coding agents running in GitHub Actions. This repo also uses Frontera
on itself: agent tickets here change the dispatcher that runs agents, so correctness and the safety
rules below matter more than usual.

## Build and test

```bash
uv sync
uv run pytest -q          # must pass; tests need no network or GitHub
```

Without uv: `python -m venv .venv && .venv/bin/pip install -e . pytest && .venv/bin/python -m pytest -q`.

## Map

| Path | Role |
|---|---|
| `src/frontera/action.py` | Actions entry point: event → owner permission check → run, chain, fix, promote. `GitHubForge` (push, PRs, merge) |
| `src/frontera/runner.py` | Ticket lifecycle: attempts, verification, PRs, promotion, review/test gates, fix loop, merge |
| `src/frontera/routing.py` | Lanes, launch policies (`auto`/`on_command`/`disabled`), reviewer choice. Pure functions |
| `src/frontera/preflight.py` | Deterministic checks before any model runs |
| `src/frontera/harness.py` | Worker CLIs (OpenCode, Claude Code): command line, scrubbed environment, timeout |
| `src/frontera/verify.py` | Independent checks of a worker's change: changed files, allowed/protected paths, scan, check command |
| `src/frontera/scan.py` | Secret and privacy patterns |
| `src/frontera/contextpack.py`, `prompts/` | What a worker is told and shown |
| `src/frontera/tracker/github.py` | Issues, labels, the bot-owned state comment |
| `src/frontera/commands.py` | `/frontera ...` grammar |
| `tests/fakes.py`, `tests/test_github.py` | In-memory GitHub and forge; end-to-end lifecycle tests over a real local git origin |
| `docs/` | Plan, routing, GitHub design, decisions. Read the relevant section, not all of it |

## Rules that must not regress

- Only accounts with admin or maintain permission can start anything. Strangers get no response and never reach a model.
- Never trust a worker's `result.json` for anything that matters; the dispatcher re-checks files, paths, scan and checks itself.
- Workers and check commands get a scrubbed environment: no GitHub token, no other provider's key.
- `on_command` endpoints run only with a quoted owner command; promotion never carries a command into a new lane.
- Only comments by the bot logins are read as state.
- Agent PRs are drafts; merging happens only on the owner's `/frontera approve` or by hand.

A change that touches one of these needs a test that would fail if the rule broke.

## Conventions

- Python 3.12, standard library first; the only runtime dependency is PyYAML.
- Every behaviour change gets a test in `tests/`. Prefer the existing fakes over mocks.
- Keep functions small and side effects injectable (tracker, forge, launcher).
- Significant design changes get an entry in `docs/DECISIONS.md`; agents may draft one but never mark it accepted.
- Public repo: no hostnames, account names, private paths or credentials in code, tests, docs or tickets.
