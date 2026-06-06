# Flat-rate-first Conductor vision becomes doctrine

**Date:** 2026-06-06
**Type:** decision
**Trigger:** T1.1, T2.1, T2.5
**Cites:** doctrine/0007-flat-rate-first-delivery-control-plane, plans/root-cause-roadmap, principles/ai-delivery-architecture.md, docs/consumers.md, README.md
**inferred-invariant:** true

> Conductor's product lane is flat-rate-first AI delivery control, with
> OpenRouter as metered overflow and Factory treated as a possible provider.

## Context

The product comparison against OpenRouter and Factory clarified the market
boundary. OpenRouter already owns broad model marketplace access and provider
routing. Factory already owns a stateful coding-agent experience with headless
execution and larger orchestration. Conductor should not compete by becoming a
worse version of either product.

The useful lane is operational: keep AI engineering workflows running across
the tools a team already pays for, normalize provider failures, and make PR
review and delegation reliable. That requires provider economics to become a
first-class routing input, not a hidden side effect of provider names.

## What we decided

Conductor is an AI delivery control plane. Default routing should prefer
plan-backed or flat-rate providers first, use local/offline providers when
explicitly appropriate, and fall back to OpenRouter as the metered gateway.

Kimi and DeepSeek remain compatibility provider IDs only as OpenRouter-backed
model presets. They must not be treated as local, plan-backed, or independent
first-hop provider ideas. Factory, if added, belongs behind the provider
contract as a plan-backed stateful-agent provider.

## Consequences / action items

- [x] Add doctrine that owns the flat-rate-first product invariant.
- [x] Update the root-cause roadmap so Provider Capability Model work includes
      provider economics and OpenRouter-overflow routing.
- [x] Update the AI delivery architecture notes so merge-gate decisions follow
      the same routing policy.
- [ ] Implement provider economics in code: classify providers, expose route
      receipts, and make semantic routing flat-rate-first by default.
