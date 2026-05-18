# Contributing to Glitch Social Media Agent

Thanks for your interest. This is a build-in-public project — contributions that improve the open-source engine are welcome.

## What's in scope

- Bug fixes in the video pipeline, scheduler, ORM, or platform integrations
- New video model clients (`src/glitch_signal/video_models/`)
- New platform publishers (`src/glitch_signal/platforms/`)
- Performance improvements to the scheduler or assembler
- Test coverage improvements

## What's out of scope

- Changes to brand voice, guardrail phrases, or watermark assets (these live in the private `brand.config.json`)
- Scope creep beyond short-form video + ORM

## Setup

```bash
git clone https://gitlab.com/glitch-grow/ai-social-agent_marketing_stack-ai-social-media-agent
cd ai_marketing_stack-ai-social-media-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run tests (no API keys needed)

```bash
DISPATCH_MODE=dry_run pytest tests/ -v
```

## Lint

```bash
ruff check src/ tests/
```

## Adding a video model

1. Create `src/glitch_signal/video_models/{model_name}.py`
2. Implement the `VideoModel` ABC from `video_models/base.py`:
   - `generate(req: VideoGenerationRequest) -> VideoGenerationResult`
   - `poll(api_job_id: str) -> VideoGenerationResult`
3. Register it in `get_model()` in `video_models/kling.py` (or extract to a factory)
4. Add a model_hint entry to `brand.config.example.json`
5. Add dry-run path (return mock result when `DISPATCH_MODE=dry_run`)
6. Add tests

## Adding a platform

1. Create `src/glitch_signal/platforms/{platform}.py`
2. Implement `upload(asset_path, metadata) -> str` (returns platform post ID)
3. Wire into `agent/nodes/publisher.py`
4. Document required env vars in `.env.example`

## Commit style

```
type: short description (≤72 chars)

Optional body. Explain why, not what.
```

Types: `fix`, `feat`, `refactor`, `test`, `docs`, `chore`

## Pull requests

- One logical change per PR
- All CI checks must pass
- No credentials in diff — ever
