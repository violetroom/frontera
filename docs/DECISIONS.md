# Decisions

Accepted means the owner chose it. It does not mean it is built or tested. To change a decision, add a new entry that links the old one rather than editing it.

| ID | Date | Decision | Status |
|---|---|---|---|
| D1 | 2026-10-14 | Tracker: Jira Cloud (plan §8, T1) | Accepted |
| D2 | 2026-10-14 | Manager: Hermes Agent on OpenAI (ChatGPT/Codex OAuth) (plan §2.3, M1) | Accepted |
| D3 | 2026-10-14 | Interaction model: the owner tells Hermes what to do; Hermes launches the worker agents | Accepted |

## D1: Jira as tracker

Jira Cloud holds work state for managed projects. GitHub Issues was considered: less setup, native PR links. Jira was chosen for real workflow states, JQL and the Rovo MCP server. The dispatcher still talks to the tracker through one adapter module.

Owner, 2026-10-14: "Jira"

## D2: Hermes with OpenAI as the manager

Hermes Agent is the conversational manager, running on the owner's ChatGPT Plus via Hermes's Codex/ChatGPT-OAuth provider. A Claude Pro login is never configured in Hermes (plan §3). Qwen may serve as a fallback model for routine triage when Plus quota runs out. Not yet decided.

Still to check: OpenAI's current terms for ChatGPT sign-in in third-party clients, and that Hermes's Codex provider works with a Plus account.

Owner, 2026-10-14: "hermes with oai"

## D3: Hermes launches the workers

The owner gives instructions to Hermes in conversation, and Hermes launches the worker agents through the `frontera` dispatcher. Hermes does not edit managed repos itself or call worker CLIs directly. The dispatcher's preflight, verification and PR steps still apply.

How this is read for the open questions:
- An instruction to Hermes to run, or approve, a ticket counts as the owner's approval. Hermes records that instruction on the ticket as a quoted comment before dispatching.
- P1 (whether Hermes may launch Claude Code runs) is resolved by D5.

Owner, 2026-10-14: "I would like to tell the hermes agent things and it will launch other agents."

## Entries after the first set

| ID | Date | Decision | Status |
|---|---|---|---|
| D4 | 2026-10-14 | Route work by task kind and difficulty tier through config-defined lanes and endpoints, with promotion on failure or timeout | Accepted direction; design in [ROUTING.md](ROUTING.md) |
| D5 | 2026-10-14 | Claude (Pro) is `on_command`: promoted tickets, hard tickets and reviews, started only by the owner's message. Resolves P1 | Accepted |
| D6 | 2026-10-14 | Codex is disabled as a coding worker while Hermes's use of the Plus account is measured | Accepted |
| D7 | 2026-10-14 | Qwen (Tellico) is the default `auto` endpoint for processing tickets | Accepted |

## D4: Kinds, tiers, lanes and endpoints

Tickets carry a kind (`code`, `research`; `review` is a dispatched action) and a tier. Config maps each `(kind, tier)` lane to an ordered list of endpoints. Each endpoint has a harness and a launch policy. Adding an OpenAI-compatible endpoint is a config change; a new harness is one adapter module. When a lane fails or runs out of time, the ticket is promoted to the next lane with a handoff note. Ambiguity and policy blocks go to Needs Decision instead.

The owner wants to be able to add model endpoints later, for example a Google model for research, or Qwen Flash Next or DeepSeek Flash for mid-difficulty tickets. These are not configured yet. Each needs a data-handling check and a trial run before real use.

Owner, 2026-10-14: "we need to be able to define certain tasks and levels and be able to assign models certain domains."

## D5: Claude on command

Claude handles promoted tickets, tickets drafted at the top tier, and code review/approval. It runs only when the owner's message to Hermes asks for it, by ticket or by a batch phrase like "review the finished tickets". The dispatcher refuses `on_command` endpoints without a quoted owner command and posts the quote on the ticket. A Claude `approve` verdict is a recommendation; the owner still merges. Avoids a paid API key for unattended use.

Known limit: the dispatcher trusts Hermes to pass the owner's command honestly. Gateway message-ID checks are a later hardening step.

Owner, 2026-10-14: "for promoted tickets and review tasks, I can send the word that we need to use claude … I am the one sending the command at that point."

## D6: Codex disabled for coding

Hermes uses the owner's ChatGPT Plus account, and Codex workers would compete for the same quota. Codex stays `disabled` until Hermes's usage is known. Re-enabling it is a config change, either to `on_command` or `auto`.

Owner, 2026-10-14: "Since we are using oai for hermes, maybe disable using it for code right now. I could always enable it if I want extra usage. I need to see how much usage hermes uses though."

## D7: Qwen processes tickets

Qwen 3.8 27B on Tellico, through OpenCode, is the `auto` endpoint for ordinary code and research tickets.

Owner, 2026-10-14: "Qwen would be in charge of processing tickets."

| ID | Date | Decision | Status |
|---|---|---|---|
| D8 | 2026-10-14 | Build Frontera's own Python dispatcher; borrow ideas from Sortie and Symphony rather than adopting them | Accepted |

## D8: Own dispatcher

Existing orchestrators were considered. [Sortie](https://pkg.go.dev/github.com/sortie-ai/sortie@v1.8.0) (Go; Jira; Claude Code, Codex and Copilot) and [OpenAI Symphony](https://openai.com/index/open-source-codex-orchestration-symphony) (a Linear and Codex reference spec) cover the polling, workspace and retry loop. Neither covers OpenCode/Qwen, per-ticket routing by kind and tier, launch policies, promotion or dispatcher-side verification. Adapting Sortie would mean writing Go, which the owner doesn't want to take on. Ideas worth borrowing: Sortie's persisted retry queues, stall detection and adapter registry; Symphony's tracker-status state machine and keeping tracker tokens away from agents.

Model-call routers (LiteLLM, OpenRouter) remain a candidate for API-key endpoints once a second one is added. They cannot route the Pro or Plus subscriptions.

Owner, 2026-10-14: "lets just do our own at that point. we can borrow ideas from sortie though. I don't know go and i dont feel like figuring it out."
