---
title: Support agent — canonical wiki scope
tags: [hammer, support, grounding]
updated: 2026-05-28
voice: published
---

# Hammer Support AI — canonical knowledge scope

This page defines the **wiki slice** the Hammer **customer support** voice and chat agent must use for factual answers.

## Allowed wiki pages

- [entity-hammer-support.md](entity-hammer-support.md) — support hub, contacts, escalation, product overview for troubleshooting
- [source-slack-support.md](source-slack-support.md) — manifest of Slack-ingested support threads
- [demo-public-site-copy.md](demo-public-site-copy.md) — browser UI strings only
- All **`*.md` under `wiki-support/topics/`** — synthesized Q&A from Slack

## Support raw corpus

- All **`*.md` under `raw/support-data/`** (recursive) may be indexed for retrieval, including Slack thread exports under `raw/support-data/slack/`.

## Out of scope

- Sales wiki (`wiki/`) and sales raw corpus (`raw/hammer-data/`) — not indexed by the support agent
- Pricing negotiation, trial signup, or agreement email flows

## Human support contact (escalation)

When the agent cannot answer from the knowledge base, or the customer asks for a person:

- **Email:** support@hammertime.com
- **Phone:** (512) 883-1336
- **Hours:** Monday–Friday, 9:00 a.m. – 5:00 p.m. Central Time (Austin, Texas)

## Agent behavior

- Answer only from retrieved excerpts and this allowlist
- Troubleshoot step-by-step; confirm resolution when possible
- Do not invent policies, prices, or integration steps not in the corpus
- Escalate politely when unsure
