# GitHub design v0

Status: **draft, 2026-10-14.** Implements [D9](DECISIONS.md) (GitHub instead of Jira) and [D10](DECISIONS.md) (agents run in Actions). Routing ([ROUTING.md](ROUTING.md)) is unchanged. This file replaces the Jira parts of [PLAN.md](PLAN.md) §2.1–2.2.

## Where things live

| Thing | Where |
|---|---|
| Tickets | Issues in the managed repo (e.g. `violetroom/Shiroe`), written with the **Agent task** issue form |
| Kind, tier, state, pin | Labels on the issue |
| Attempts, promotions, last endpoint, PR number, handoff notes | One **state comment** on the issue, written and edited only by the workflow bot |
| Routing config | `.github/frontera.yaml` in the managed repo. Public; no secrets, only env-var names |
| Dispatcher | The `frontera` package, installed in the job from `violetroom/frontera` at a pinned ref |
| Workflow logic | Reusable workflow `violetroom/frontera/.github/workflows/agent.yml`, called by a small `frontera.yml` in the managed repo |
| Secrets | Managed-repo Actions secrets: `TELLICO_GATEWAY_URL`, `TELLICO_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` |
| Run logs | Workflow artifact (manifest, result, truncated agent output). **Public repos: anyone signed in can download these** |
| Board | Optional GitHub Project over the issues. A view only; labels are the source of truth |

## Labels

| Label | Meaning | Set by |
|---|---|---|
| `kind:code`, `kind:research` | Task kind | The issue form (auto-applied) or you |
| `tier:1` … `tier:3` | **Current** tier. The form records the original | You; the bot on promotion |
| `state:ready` | Approved for an agent. **Adding it triggers a run** | You |
| `state:working` | A run is in progress | Bot |
| `state:awaiting-command` | Next endpoint is `on_command` (e.g. Claude). Waits for `/frontera run` | Bot |
| `state:in-review` | Draft PR open | Bot |
| `state:changes-requested` | Review asked for changes | Bot |
| `state:needs-decision` | Ambiguous, blocked by policy, or out of tiers | Bot |
| `state:failed` | Infrastructure failure; re-add `state:ready` to retry | Bot |
| `on:<endpoint>` | Which endpoint is running it. Counts toward `max_concurrent` | Bot |
| `pin:<endpoint>` | Force an endpoint | You |
| `risk:high` | Runs only on an explicit `/frontera run` | You |
| `review:approve` / `review:changes` / `review:block` / `review:self` | Last review verdict | Bot |

An issue with no `state:` label is Backlog. A closed issue is Done; `Closes #N` in the agent PR closes it on merge.

## Who can trigger a run

The repos are public, so anyone can open issues and comment. Before doing anything, the dispatcher checks **the account that caused the event** through the GitHub API (`collaborators/{login}/permission`). Only `admin` or `maintain` is accepted. The workflow's `if:` also filters on `author_association` as a cheap first check. A stranger's comment or issue never starts a job, and never reaches a model.

| Event | Who | What happens |
|---|---|---|
| Label `state:ready` added | Owner | Route the ticket. `auto` endpoint: run it. `on_command` endpoint: set `state:awaiting-command` and say so |
| Comment `/frontera run [endpoint]` (on the ticket, or on its agent PR for any command) | Owner | Run now. The comment is the **owner command** (quoted and linked), so `on_command` endpoints like Claude may run |
| Comment `/frontera review` | Owner | Review the ticket's PR with the review lane (Claude, falling back to Qwen) |
| Comment `/frontera fix` | Owner | After `changes`: re-run at the current tier with the review in the handoff |
| Comment `/frontera promote` | Owner | Move to the next tier by hand |
| `workflow_dispatch` `run-awaiting` / `review-pending` | Owner | Batch versions: "do the promoted tickets", "review the finished tickets" |

The owner command is now authenticated by GitHub itself, which closes the [D5](DECISIONS.md) "dispatcher trusts Hermes" gap. When Hermes arrives, it gets its own GitHub App identity. Its comments are visibly not yours, and it can only start `auto` runs and draft tickets unless you post the command. Relaying your chat instructions as owner commands is a later, explicit decision.

## A run, step by step

1. Checkout of the managed repo's default branch, without persisted git credentials.
2. `frontera action` reads the event, checks permission, and loads the ticket from the issue.
3. Route and preflight ([ROUTING.md](ROUTING.md)). Endpoint capacity is counted from open issues labeled `state:working` + `on:<endpoint>`.
4. Labels → `state:working` + `on:<endpoint>`. The state comment records the execution ID and a link to the run.
5. Branch `agent/<execution-id>`, context pack in `.frontera/` (git-excluded), worker launched with the lane's `wall_clock` as a hard timeout. **The worker's environment is an allowlist:** `PATH`, `HOME` and the like, plus only its own provider secret. `GH_TOKEN`, other providers' keys and runner tokens are removed.
6. Verify, without trusting `result.json`: changed files must match Allowed Paths, the diff passes the secret and privacy scan, and the check command passes. Research tickets must change only the report file.
7. Outcome:
   - **Success:** commit, push, open a draft PR with `Closes #N`, → `state:in-review`.
   - **`needs_decision` / `policy_blocked` / scope or scan violation:** → `state:needs-decision`, with the reasons and questions in a comment.
   - **Failed, timed out, checks failed, `needs_higher_tier`:** attempts + 1 with a handoff note. Retry while attempts remain, otherwise **promote**. If the promoted lane is `auto`, keep going in the same job. If it's `on_command`, → `state:awaiting-command` and comment "reply `/frontera run`".
   - **Rate limit / outage / harness crash:** → `state:failed` (transient), no attempt counted.
8. **Drain:** before exiting, the job picks up other `state:ready` tickets whose `auto` endpoint has free capacity, up to a small limit. This is needed because label changes made by the workflow's own token don't trigger new workflows.

## Review

`/frontera review` checks out the PR branch read-only and gives the reviewer the ticket, the criteria and `git diff base...branch`. It must write `{verdict, summary, comments[]}`. The dispatcher posts a PR review with event `COMMENT`, never approve, plus a `review:` label:
- `approve`: stays in review. You merge.
- `changes`: → `state:changes-requested`.
- `block`: → `state:needs-decision`.

The reviewer differs from the author where possible ([ROUTING.md](ROUTING.md#review)).

## Known limits

- **CI doesn't run on agent PRs yet.** PRs created with the workflow's `GITHUB_TOKEN` don't trigger other workflows. The dispatcher runs the check command itself and reports it on the PR. Fix later with a GitHub App token.
- **Agents have open network access** on hosted runners. The VM is disposable, but a prompt-injected worker could send out its own provider token. Mitigation: only owner-approved tickets run, and the tickets are written by you or Hermes. Rotate the Claude token or Tellico key if a run looks wrong.
- **Usage-limit cooling isn't shared between jobs** yet. A rate-limited run just fails as transient.
- **Everything in issues, PRs and run artifacts is public.** Follow the managed repo's privacy rules (Shiroe 0013) in ticket text. The scanner blocks known patterns, not everything.
- The owner must enable **Settings → Actions → "Allow GitHub Actions to create and approve pull requests"** (org and repo), or PR creation fails.

## Setting up a managed repo

1. Copy `templates/managed-repo/.github/` into the repo: the workflow, the issue form and `frontera.yaml`. Edit `frontera.yaml`: project key, `github_repo`, `check_command`.
2. `frontera --config <repo>/.github/frontera.yaml labels --repo <owner/name>` creates the labels.
3. Add the secrets the endpoints name: `TELLICO_GATEWAY_URL`, `TELLICO_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`. Optionally add `FRONTERA_PRIVATE_TERMS`, one term per line, for the privacy scanner.
4. Org and repo **Settings → Actions → General**: allow GitHub Actions to create pull requests. Allow `violetroom/frontera`'s reusable workflow, which is the default inside the same org.
5. Optional: a ruleset on `main` requiring a pull request and your review.

## Validation so far

- 2026-10-14, local: the `opencode` adapter drove Qwen 3.8 27B through the Tellico gateway on a scratch repo ticket ("add `subtract()` with a test"). It finished in 19 s with a valid `result.json`, changes inside Allowed Paths, and the independent check passed. This exposed `__pycache__` files being counted as changes, which is now filtered.
- Not yet run: the `claude-code` adapter, which needs `CLAUDE_CODE_OAUTH_TOKEN` and is first exercised in Actions, and any end-to-end run in Actions.
