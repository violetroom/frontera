# Routing v0: task kinds, tiers, lanes and endpoints

Status: **draft, 2026-10-14.** Lane resolution, launch policies, promotion targets and reviewer choice are implemented in `src/frontera/routing.py` (dry run only). Promotion and run execution start in step 3. This replaces the fixed provider table in [PLAN.md §2.4](PLAN.md#24-workers-and-routing). See [D4–D7](DECISIONS.md).

## Goal

Decide which model works on which ticket from a config file, not from code, so that:

- Qwen processes ordinary tickets on its own.
- Claude handles promoted tickets, tickets drafted as hard, and reviews, but only when you say so.
- Codex is switched off for coding while Hermes's usage of the same Plus account is measured, and can be switched back on with a single setting.
- Adding a new endpoint, such as Qwen Flash Next, DeepSeek Flash or a Google model for research, is a config entry, plus a small adapter if it needs a new harness.

## Concepts

```
 Ticket ──(kind, tier)──► Lane ──(ordered list)──► Endpoint ──(harness)──► run
                            ▲                         │
                            └──── promotion ◄─────────┘ (failure / timeout)
```

| Concept | What it is | Example |
|---|---|---|
| **Kind** | What sort of work, which fixes the output contract and how it is checked | `code`, `research`, `review` |
| **Tier** | How hard the ticket is, 1–N | T1 mechanical, T2 moderate, T3 hard |
| **Lane** | The `(kind, tier)` pair mapped to an ordered list of endpoints, plus budgets | `code/T1 → [qwen-27b]` |
| **Endpoint** | One model reachable through one harness, with a launch policy and limits | `claude-pro`: Claude Code, `on_command` |
| **Harness** | The program that runs an agent loop for a model. A code adapter in `frontera` | `opencode`, `claude-code`, `codex`, `api` |
| **Launch policy** | Who may start a run on that endpoint | `auto`, `on_command`, `disabled` |

### Kinds

| Kind | Output | Verified by the dispatcher | Network |
|---|---|---|---|
| `code` | Diff, then a draft PR | Diff stays inside Allowed Paths, secret/privacy scan, project check command, CI | Denied |
| `research` | One Markdown report at a set path (e.g. `docs/research/SHI-31.md`), then a PR | Only that file changed; it has a Sources section; privacy scan | **Allowed** (web search), so research endpoints get no other write access |
| `review` | Review of an open agent PR: verdict + comments | Verdict is one of `approve`/`changes`/`block`, each comment cites a file and line | Denied; read-only worktree |
| `test` | Execution report on an open agent PR: verdict + per-criterion evidence | Project check passes on the PR head; no repository file changed; verdict `pass`/`fail` | Denied; scratch files only |

Later kinds, such as `docs` or `decision-draft`, are added the same way: an output contract plus a verification recipe. Hermes keeps drafting tickets and decision text itself in conversation. That is not a dispatched kind.

### Tiers

Hermes proposes a tier when it drafts a ticket, and you can change it. Guidance:

| Tier | Meaning |
|---|---|
| **T1** | Mechanical or local: one area, precise criteria, the pattern already exists in the repo |
| **T2** | Moderate: several files, some design within accepted decisions |
| **T3** | Hard: cross-cutting, architecture-sensitive, subtle correctness, or failed at a lower tier |

The number of tiers is set in config. Add T2 lanes as you get mid-strength endpoints, such as Qwen Flash Next or DeepSeek Flash.

## Launch policies

| Policy | Who can start a run | Use |
|---|---|---|
| `auto` | The dispatcher, for any approved ticket: when Hermes decides on its own, from a schedule, or in a batch | Free or cheap endpoints (Qwen) |
| `on_command` | Only when your message to Hermes names the ticket, the endpoint/lane, or a batch command that covers it | Subscription endpoints you want attended (Claude) |
| `disabled` | Nobody. The lane skips the endpoint | Codex for now |

A ticket whose next lane endpoint is `on_command` stops in **Awaiting Command** and does nothing until you say so. This covers both promoted tickets and tickets drafted straight in at T3.

**How "you said so" is enforced:** Hermes calls `frontera run … --owner-command "<your message, quoted>"`. The dispatcher refuses an `on_command` endpoint without it, and posts the quote on the ticket before launching. **Known limit:** the dispatcher can't prove the quote came from you, because Hermes is trusted to pass it honestly. A later hardening step: Hermes also passes the gateway message ID, and the dispatcher checks it against the gateway log.

Commands you can give Hermes, and what they map to:

| You say | Hermes runs |
|---|---|
| "review the finished tickets" | `frontera review --pending --owner-command "…"` |
| "do the promoted tickets" / "run the waiting ones" | `frontera run --awaiting-command --owner-command "…"` |
| "run SHI-12 on claude" | `frontera run SHI-12 --endpoint claude-pro --owner-command "…"` |
| "approve SHI-14" | Moves the ticket to Ready. If its lane endpoint is `auto`, it starts |

Batches run one ticket at a time on an endpoint, stop at the endpoint's `max_runs_per_command`, and stop at the first usage-limit error. The rest stay in Awaiting Command.

## Promotion

A ticket is **promoted** (tier + 1, Promotions + 1) when its current lane can't finish it:

| Trigger | Example |
|---|---|
| Wall-clock budget exceeded | Qwen ran more than 45 minutes at T1 |
| Attempts exhausted with failed verification | Tests still failing after 2 tries |
| Worker reports `needs_higher_tier` | "Needs changes across the storage layer I can't complete" |

**Not** promotion triggers, because they go elsewhere:
- Ambiguous or underspecified criteria → **Needs Decision**. Promoting a vague ticket just spends Claude quota on the same ambiguity.
- Policy block, meaning a protected path or a secret → **Needs Decision**.
- Infrastructure trouble (Tellico 503, 429, provider outage) → retry later at the same tier.

When a ticket is promoted, the next run's context pack also gets a **handoff note**: the earlier attempts' `result.json` summaries, failure reasons, the last check output (truncated), and the name of the abandoned branch, which is kept for reference. The higher tier starts from what was learned, not from scratch.

Each lane has a `promote_to` setting. When there is no higher lane, the ticket goes to **Needs Decision** for you to handle.

## Review

- `frontera review` targets tickets in **In Review** with an open draft PR and no review verdict yet.
- The reviewer gets the ticket, the acceptance criteria, the diff and the context pack, in a **read-only** worktree.
- Rule: the reviewing endpoint should differ from the authoring endpoint. A Claude-authored PR is either reviewed by the next-best review endpoint or flagged `self-review` for you to look at more closely.
- The verdict goes on the PR as a comment and in the ticket's Review Verdict field:
  - `approve`: a recommendation. **You still merge.** The dispatcher never gets merge rights.
  - `changes`: the ticket goes to **Changes Requested**. "fix SHI-12" re-runs it at its current tier, with the review comments added to the context pack.
  - `block`: a security, privacy or scope problem. The ticket goes to **Needs Decision**.

Letting `approve` plus green CI auto-merge low-risk tiers is a possible later step (the report's "L3"). It is out of scope now.

## Config

The routing policy and endpoints go in the user config at `~/.config/frontera/config.yaml` (or `$FRONTERA_CONFIG`), never in the repo, because endpoint URLs and account choices are personal. The repo ships [`config.example.yaml`](../config.example.yaml), which also holds the tracker, project and privacy settings.

```yaml
tiers: [1, 2, 3]

endpoints:
  qwen-27b:
    harness: opencode
    model: tellico-0/qwen3.8-27b-gsq-iq3s     # from the tellico-agentic-setup config
    launch: auto
    max_concurrent: 1          # one OpenCode session already uses several cluster slots
    projects: [SHI]
  claude-pro:
    harness: claude-code
    model: default             # whatever the Pro plan provides
    launch: on_command
    max_concurrent: 1
    max_runs_per_command: 3
  codex-plus:
    harness: codex
    launch: disabled           # D6: re-enable after Hermes usage is measured
  # Examples of later additions (not configured yet):
  # qwen-flash-next:
  #   harness: opencode
  #   provider: { base_url_env: QWEN_FLASH_URL, api_key_env: QWEN_FLASH_KEY }
  #   launch: auto
  # gemini-research:
  #   harness: api
  #   provider: google
  #   tools: [web_search]
  #   launch: auto
  #   budget: { max_usd_per_run: 0.50 }

lanes:
  code/1:   { endpoints: [qwen-27b],   wall_clock: 45m, attempts: 2, promote_to: code/3 }
  code/2:   { endpoints: [qwen-27b],   wall_clock: 90m, attempts: 2, promote_to: code/3 }
  code/3:   { endpoints: [claude-pro], wall_clock: 60m, attempts: 1 }
  research/1: { endpoints: [qwen-27b], wall_clock: 30m, attempts: 1, promote_to: research/3 }
  research/3: { endpoints: [claude-pro], wall_clock: 45m, attempts: 1 }
  review:   { endpoints: [claude-pro], fallback: [qwen-27b] }
  test:     { endpoints: [qwen-27b], wall_clock: 30m }
```

`code/1` promotes straight to `code/3` for now, because no endpoint is stronger than Qwen and weaker than Claude yet. When you add, say, `qwen-flash-next`, put it in `code/2` and change `code/1`'s `promote_to` to `code/2`. No code changes.

Lane resolution goes through the list in order. It skips `disabled` endpoints, endpoints not allowed for the project, endpoints cooling after a usage limit, and endpoints at `max_concurrent`. If the first usable endpoint is `on_command` and there is no owner command, the ticket goes to Awaiting Command.

## Tellico setup, remote control and plugin env

An `opencode` endpoint can use the pinned Tellico setup that the workflow checks out. All of these are provider settings in `.github/frontera.yaml`:

```yaml
provider:
  setup_dir_env: TELLICO_SETUP_DIR          # env var naming the setup checkout
  env_passthrough: [TELLICO_LOOP_GUARD, TELLICO_DISK_MIN_FREE]
```

- **`setup_dir_env`.** When the variable is set and the directory exists, the generated OpenCode config reads `<dir>/config/opencode.json` (JSONC) and takes only: the headless-safe plugins `loop-guard`, `compaction-guard`, `secret-guard`, `disk-guard` (as `<dir>/plugins/<name>`; `tui`, `tui-v2`, `dispatch-balance` are dropped), `tool_output`, `compaction`, and the setup's model block for the endpoint's model (variants, options). Frontera's own limits and `model_config` are applied on top. `baseURL`, `apiKey`, `permission`, `autoupdate` and `share` are always Frontera's; nothing from the setup can loosen them. With the variable unset or the directory missing the config is the minimal one. A malformed setup config fails the launch with the file path.
- **Remote control.** If `TELLICO_REMOTE_CONTROL=1`, `TELLICO_RELAY_URL` and `TELLICO_RELAY_KEY_FILE` are set (the workflow's `remote_control` input) and the setup dir exists, the `remote-control` plugin is loaded and exactly those three plus `TELLICO_RELAY_MACHINE` reach the worker. The machine name is the workflow default followed by the execution id, so the app shows which session is which. Otherwise neither the plugin nor the variables are passed. Every `launch.json` records `remote_control: true|false`. Publishing a headless session is confirmed; steering it from the app is not yet.
- **`env_passthrough`.** Extra variables (for plugin tuning) copied into the worker's scrubbed environment only when they are set. Dispatcher credentials (`GH_TOKEN` etc.) are refused.

Enabling `setup_dir_env` edits a protected path, so the owner makes that change after merge.

## Adding a model

1. **OpenAI-compatible endpoint** (DeepSeek, Qwen Flash, OpenRouter, Gemini's OpenAI-compatible endpoint, another Tellico model): add an `endpoints:` entry using the `opencode` harness and reference its key by environment variable, then put it in a lane. No code.
2. **New harness** (e.g. the Gemini CLI, or a plain-API research loop): write an adapter in `frontera/harnesses/`:

   ```python
   class Harness(Protocol):
       name: str
       def supports(self, kind: str) -> bool: ...             # code / research / review
       def launch(self, run: RunSpec) -> RunHandle: ...       # worktree, pack, prompt, sandbox, timeout
       def usage(self, handle: RunHandle) -> Usage: ...       # tokens / cost / wall time, as available
       def classify_error(self, handle: RunHandle) -> ErrorClass: ...  # rate_limit / outage / semantic / timeout
   ```

   The `api` harness is the generic one for research-only endpoints. It sends one request with the provider's built-in web search or grounding and writes the report file. It never gets a shell.
3. **Before using a new endpoint on a project,** check its data handling. Code and ticket text are sent to that provider. Use `projects:` to keep an endpoint off projects it shouldn't see.

Each new endpoint starts on a trial: run 3–5 tickets already completed elsewhere and compare the results before it handles real work.

## Usage measurement

| Source | What is recorded |
|---|---|
| Worker runs | Per run: endpoint, kind, tier, wall time, outcome, and tokens/cost where the harness reports them (`claude -p --output-format json` and `codex exec --json` report usage; check what `opencode run` exposes) |
| Usage-limit errors | Time and endpoint. Sets the endpoint to cooling until its reset time |
| **Hermes** | Unknown. Hermes's own token/usage reporting needs checking in step 1b. Fallback: watch the ChatGPT/Codex usage page and count Hermes rate-limit hits |

`frontera usage` summarizes this per endpoint, per lane and per week. Use it to decide when to re-enable Codex, and which tier each ticket type belongs in. For example: if most T1 tickets of a type get promoted, draft that type at a higher tier.

## Jira changes

*Superseded by D9: GitHub labels and the state comment carry these fields. See [GITHUB.md](GITHUB.md#labels).* Kept for the record:

| Field | Values |
|---|---|
| Task Kind | code / research |
| Tier | 1 / 2 / 3 (initial difficulty) |
| Current Tier | Updated on promotion |
| Promotions | int |
| Pin Endpoint | Optional; forces a specific endpoint (still subject to its launch policy) |
| Last Endpoint | Set by the dispatcher |
| Review Verdict | none / approve / changes / block / self-review |

```
Backlog → Ready → Agent Working → In Review ──(review)──► approve: you merge → Done
            │          │   │            └──► changes → Changes Requested ──("fix")──► Ready
            │          │   └→ promoted ──► Awaiting Command ──(your command)──► Agent Working
            │          └→ Needs Decision ──(you answer)──► Ready
            └→ Awaiting Command   (when the lane's endpoint is on_command, e.g. drafted at T3)
```
