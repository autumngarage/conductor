---
Status: Proposed
Date: 2026-06-06
Load-priority: always
Promoted-from: journal/2026-06-06-flat-rate-first-conductor-vision.md
Cites: principles/ai-delivery-architecture.md, plans/root-cause-roadmap.md, README.md, docs/consumers.md
Grounds-in: principles/documentation-ownership.md
---

# 0007 - Conductor is a flat-rate-first AI delivery control plane

> Conductor's durable lane is reliable dispatch across already-paid agents,
> local providers, and metered gateways, with OpenRouter used as overflow
> rather than as the first default path.

## Context

Conductor sits next to stronger adjacent products. OpenRouter is the model
marketplace and metered gateway. Factory AI is a stateful coding-agent product
with headless execution, missions, and its own routing layer. Conductor loses
clarity if it tries to out-marketplace OpenRouter or out-agent Factory.

The durable pain Conductor can own is narrower: keep AI engineering delivery
workflows running across the tools a team already pays for. A driving CLI,
Touchstone merge gate, Sentinel role, or repo automation should be able to ask
for work, review, or recovery without knowing every provider's auth model,
timeout shape, failure mode, or cost semantics.

The current docs already describe Conductor as the adapter and routing layer,
but they do not make provider economics a first-class design constraint. That
gap lets OpenRouter-backed model presets, local providers, and plan-backed CLI
agents appear equivalent in routing discussions even though their cost and
failure tradeoffs are materially different.

## Decision

Conductor is an AI delivery control plane, not a replacement for provider
agents or model gateways.

Default routing will prefer providers in this economic order when they can
satisfy the same job contract:

1. Plan-backed or flat-rate stateful agents, such as Codex, Claude, Gemini, and
   Factory if a Factory adapter is added.
2. Explicit local or offline paths, such as Ollama, when the user requests
   local execution or the route is safe for local quality.
3. Metered gateway overflow through OpenRouter.

OpenRouter remains a first-class provider, but its default role is overflow,
model-library access, and explicit hard-pinned use. Kimi and DeepSeek are
OpenRouter model presets for compatibility and convenience. They are not local
providers, plan-backed providers, or default first-hop routing choices.

Factory, if integrated, should be represented as a plan-backed stateful-agent
provider behind the Conductor provider contract. Conductor should not route to
Factory so Factory can route back to OpenRouter by default; stacked routers make
costs and failures harder to explain.

## Working rules

- New provider work must classify the provider's billing/economics boundary:
  plan-backed, local, or metered gateway.
- Semantic routing should lead with plan-backed providers when capability and
  quality are sufficient, then local/offline when appropriate, then OpenRouter.
- Review gates should prefer Codex and Claude before OpenRouter-hosted review
  unless the caller explicitly pins OpenRouter or a required capability only
  exists there.
- Metered gateway use should be visible in route diagnostics so operators can
  tell when Conductor spent incremental money and why.
- Kimi and DeepSeek compatibility IDs must not cause future agents to describe
  them as local, flat-rate, or independent first-class backends.
- Provider quirks still live in adapters. The shared router should reason over
  stable capabilities and economics, not provider-name branches.

## Consequences

- **What becomes easier:** product decisions have a clear test: does this make
  AI delivery more reliable, cheaper by default, or easier to audit across
  existing providers?
- **What becomes harder:** Conductor cannot treat every provider identifier as a
  symmetric peer. The router and docs need an explicit economics model.
- **What this forecloses:** building Conductor as a general agent product, a
  broad model marketplace, or a pile of manual routing flags that operators
  must tune on every workflow.
