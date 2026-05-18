# AI Social Agent

Open-source AI agent for autonomous short-form social posting + ORM
(online reputation management) across YouTube Shorts, Instagram Reels,
X/Twitter, and LinkedIn.

## What it does

- **Scout** — periodically scans configured sources for new content
  signals worth posting about.
- **Script writer** — LLM generates a caption + script for each signal,
  matched to platform conventions (length, hashtags, tone).
- **Video pipeline** — pluggable video generation (Kling, Veo, fal.ai)
  with caption burn-in.
- **Publisher** — uploads finished media to YouTube Shorts, Instagram
  Reels, X, LinkedIn with proper metadata.
- **ORM** — listens for mentions/replies on watched accounts, drafts
  responses, optional auto-reply within guardrails.
- **Sheet-driven pacing** — one post per (brand × platform) per N hours,
  daily cap configurable.

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

## Install

```
uv pip install -e .
cp .env.example .env
alembic upgrade head
```

## License

MIT — see `LICENSE`.
