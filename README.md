# AI Social Agent

[![License: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
[![Part of Mesh Pilot](https://img.shields.io/badge/Mesh%20Pilot-stack-black.svg)](https://meshpilot.app)
[![Mirrored on Codeberg](https://img.shields.io/badge/codeberg-mirror-black.svg)](https://codeberg.org/Glitch_Exec_Lab/ai-social-agent)

> **Part of the [Mesh Pilot](https://meshpilot.app) open-source 6-agent marketing stack.**
> Autonomous short-form social posting + online-reputation management across YouTube Shorts, Instagram Reels, X/Twitter, and LinkedIn — with a human-in-the-loop approval gate before anything goes public.

The agent scans configured signal sources, drafts a caption + script tuned to each platform's conventions, generates the video, and queues the finished post for approval. ORM mentions on watched accounts get drafted responses gated to operator approval too.

## Quick start

```bash
git clone https://gitlab.com/mesh-pilot/ai-social-agent.git
# or: git clone https://codeberg.org/Glitch_Exec_Lab/ai-social-agent.git
cd ai-social-agent

uv pip install -e .          # or: pip install -e .
cp .env.example .env         # platform tokens + LLM keys + video provider
alembic upgrade head

python -m social_signal.server
```

## What it does

- **Scout** — periodically scans configured sources for new content signals worth posting about.
- **Script writer** — LLM generates a caption + script for each signal, matched to platform conventions (length, hashtags, tone).
- **Video pipeline** — pluggable video generation (Kling, Veo, fal.ai) with caption burn-in.
- **Publisher** — uploads finished media to YouTube Shorts, Instagram Reels, X, LinkedIn with proper metadata.
- **ORM** — listens for mentions/replies on watched accounts, drafts responses, optional auto-reply within guardrails.
- **Sheet-driven pacing** — one post per (brand × platform) per N hours, daily cap configurable.

## The HITL pattern (shared across the stack)

Every action that touches brand voice or public delivery routes through a human-in-the-loop approval gate. Posts are drafted, queued, and only publish after explicit operator approval. ORM replies follow the same pattern — never an auto-reply on a sensitive thread without sign-off. The audit log records who approved what, when, on which channel.

## Layout

```
src/social_signal/
  agent/      # LangGraph nodes (scout, script, publish, ORM)
  db/         # SQLModel models + async engine
  publishers/ # YouTube + Instagram + X + LinkedIn clients
  server.py   # FastAPI webhook receiver + metrics
alembic/      # database migrations
ops/          # systemd unit templates
```

## Companions in the stack

| Agent | Domain | Repo |
|---|---|---|
| AI Ads Agent | Meta / Google / TikTok / Amazon Ads | [mesh-pilot/ai-ads-agent](https://gitlab.com/mesh-pilot/ai-ads-agent) |
| AI Sales Agent | Outbound B2B sales | [mesh-pilot/ai-sales-agent](https://gitlab.com/mesh-pilot/ai-sales-agent) |
| **AI Social Agent** | This repo | — |
| AI UGC Agent | Vertical video ad pipeline | [mesh-pilot/ai-ugc-agent](https://gitlab.com/mesh-pilot/ai-ugc-agent) |
| AI Voice Agent | LiveKit-based phone agent | [mesh-pilot/ai-voice-agent](https://gitlab.com/mesh-pilot/ai-voice-agent) |
| AI SEO Agent | Shopify SEO autopilot | [mesh-pilot/ai-seo-agent](https://gitlab.com/mesh-pilot/ai-seo-agent) |

In production they're orchestrated by **[Mesh Pilot](https://meshpilot.app)** — the closed-source cockpit that runs all six in concert with shared brand context, a single web approval inbox, and cross-agent handoffs.

## Mirrors

- GitLab: [`mesh-pilot/ai-social-agent`](https://gitlab.com/mesh-pilot/ai-social-agent)
- Codeberg: [`Glitch_Exec_Lab/ai-social-agent`](https://codeberg.org/Glitch_Exec_Lab/ai-social-agent)

## Contributing

Bug reports + PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution shape (issue-first for non-trivial changes, preserve the HITL gate, conventional commits).

## Security

Security reports go to `support@meshpilot.app` — see [SECURITY.md](SECURITY.md). Please do not open public issues for vulnerabilities.

## Code of conduct

Be kind, stay on scope — see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) — fork it, ship products with it, no attribution required.

---

Built by [Mesh Pilot](https://meshpilot.app).
