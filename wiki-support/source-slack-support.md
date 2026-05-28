---
title: Slack support channel — source manifest
tags: [hammer, support, slack, provenance]
updated: 2026-05-28
---

# Slack support Q&A sources

Customer support Q&A is ingested from the internal Slack support channel via automated sync.

## Raw thread files

- Location: `raw/support-data/slack/*.md`
- One file per Slack thread (immutable export)
- Frontmatter includes `slack_ts`, `participants`, `date`

## Synthesized topics

- Location: `wiki-support/topics/*.md`
- GPT-synthesized Q&A sections grouped by product area
- Updated when Slack sync runs

## Sync status

Run `POST /api/admin/support/knowledge/slack/sync` from the Support Control dashboard, or use the CLI script in `demo/realtime-support-demo/server/slack_sync.py`.
