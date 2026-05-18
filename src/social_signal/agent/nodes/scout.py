"""Scout node — mines GitHub commits and MILESTONES.md for novel signals.

Cron-triggered. Writes Signal rows for novelty_score >= 0.6.
Persists ScoutCheckpoint rows so repeated runs don't re-score the same commits.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime

import httpx
import litellm
import structlog
from sqlmodel.ext.asyncio.session import AsyncSession
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from social_signal.agent.llm import pick
from social_signal.agent.state import SignalAgentState
from social_signal.config import settings
from social_signal.db.models import ScoutCheckpoint, Signal
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

NOVELTY_THRESHOLD = 0.6
GITHUB_API = "https://api.github.com"


async def scout_node(state: SignalAgentState) -> SignalAgentState:
    """Entry-point node. Discovers new signals AND picks the next one to process.

    Writes Signal rows to the DB (novelty_score >= 0.6), then selects the
    highest-scored unprocessed signal for this brand and sets signal_id in
    state so downstream nodes (text_writer / script_writer) have something to
    work on. One graph run == one signal processed. Multiple new signals from
    one scout are drained across subsequent runs.
    """
    from sqlmodel import select

    from social_signal.db.models import Signal as SignalTable

    brand_id = state.get("brand_id") or settings().default_brand_id
    factory = _session_factory()
    async with factory() as session:
        signals = await _run_scout(session, brand_id=brand_id)
        await session.commit()

        # Pick the next unprocessed signal for this brand. We look at the DB
        # rather than `signals` alone so we drain past leftovers too.
        result = await session.execute(
            select(SignalTable)
            .where(SignalTable.brand_id == brand_id)
            .where(SignalTable.status == "queued")
            .where(SignalTable.novelty_score >= NOVELTY_THRESHOLD)
            .order_by(SignalTable.novelty_score.desc(), SignalTable.created_at.asc())
            .limit(1)
        )
        next_signal = result.scalars().first()
        next_id = next_signal.id if next_signal else ""
        if next_signal:
            # Claim it — subsequent runs skip this one
            next_signal.status = "scripting"
            session.add(next_signal)
            await session.commit()

    log.info(
        "scout.done",
        new_signals=len(signals),
        brand_id=brand_id,
        picked_signal_id=next_id[:12] if next_id else None,
    )
    return {
        **state,
        "brand_id": brand_id,
        "signals": signals,
        "signal_id": next_id,
    }


async def _run_scout(session: AsyncSession, brand_id: str) -> list[dict]:
    found: list[dict] = []

    repos = settings().github_repo_list
    if not repos:
        repos = await _list_org_repos()

    for repo in repos:
        found.extend(await _scout_commits(session, repo, brand_id=brand_id))
        found.extend(await _scout_milestones(session, repo, brand_id=brand_id))

    return found


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _gh_headers() -> dict:
    tok = settings().github_token
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def _list_org_repos() -> list[str]:
    """List repos for the configured GitHub owner.

    Handles both account types transparently: /orgs/{x}/repos returns 404 on
    user accounts, so we fall back to /users/{x}/repos if the org endpoint
    misses. <your-org> is a user account, not an org — this matters.
    """
    owner = settings().github_org
    params = {"per_page": 100, "sort": "pushed"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GITHUB_API}/orgs/{owner}/repos",
            headers=_gh_headers(),
            params=params,
        )
        if resp.status_code == 404:
            resp = await client.get(
                f"{GITHUB_API}/users/{owner}/repos",
                headers=_gh_headers(),
                params=params,
            )
        resp.raise_for_status()
    return [r["name"] for r in resp.json() if not r.get("archived")]


async def _scout_commits(
    session: AsyncSession, repo: str, brand_id: str
) -> list[dict]:
    source_key = f"github:{repo}"
    checkpoint = await session.get(ScoutCheckpoint, source_key)

    params: dict = {"per_page": 20}
    if checkpoint and checkpoint.last_checked_at:
        params["since"] = checkpoint.last_checked_at.isoformat() + "Z"

    org = settings().github_org
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{org}/{repo}/commits",
            headers=_gh_headers(),
            params=params,
        )
        if resp.status_code == 409:
            # Empty repo
            return []
        resp.raise_for_status()
    commits = resp.json()
    if not commits:
        return []

    signals: list[dict] = []
    for commit in commits:
        sha = commit["sha"]
        message = commit.get("commit", {}).get("message", "").strip()
        if not message or len(message) < 10:
            continue

        score, summary = await _score_novelty(
            f"[{repo}] {message[:500]}", source_type="commit"
        )
        if score >= NOVELTY_THRESHOLD:
            sig = Signal(
                id=str(uuid.uuid4()),
                brand_id=brand_id,
                source="github",
                source_ref=sha,
                summary=summary,
                novelty_score=score,
                status="queued",
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(sig)
            signals.append({"id": sig.id, "summary": sig.summary, "score": score})

    # Update checkpoint
    now = datetime.now(UTC).replace(tzinfo=None)
    if checkpoint:
        checkpoint.last_checked_at = now
        checkpoint.last_ref = commits[0]["sha"] if commits else checkpoint.last_ref
    else:
        session.add(
            ScoutCheckpoint(
                source_key=source_key,
                brand_id=brand_id,
                last_checked_at=now,
                last_ref=commits[0]["sha"] if commits else None,
            )
        )

    return signals


async def _scout_milestones(
    session: AsyncSession, repo: str, brand_id: str
) -> list[dict]:
    source_key = f"milestones:{repo}"
    checkpoint = await session.get(ScoutCheckpoint, source_key)

    org = settings().github_org
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{org}/{repo}/contents/MILESTONES.md",
            headers=_gh_headers(),
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    data = resp.json()
    current_sha = data.get("sha", "")

    # Skip if SHA unchanged
    if checkpoint and checkpoint.last_ref == current_sha:
        return []

    content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
    # Take the first 2000 chars — enough novelty signal without burning tokens
    excerpt = content[:2000]

    score, summary = await _score_novelty(
        f"[{repo} MILESTONES.md] {excerpt}", source_type="milestones"
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    if checkpoint:
        checkpoint.last_checked_at = now
        checkpoint.last_ref = current_sha
    else:
        session.add(
            ScoutCheckpoint(
                source_key=source_key,
                brand_id=brand_id,
                last_checked_at=now,
                last_ref=current_sha,
            )
        )

    if score < NOVELTY_THRESHOLD:
        return []

    sig = Signal(
        id=str(uuid.uuid4()),
        brand_id=brand_id,
        source="milestones",
        source_ref=f"{repo}:{current_sha[:8]}",
        summary=summary,
        novelty_score=score,
        status="queued",
        created_at=now,
    )
    session.add(sig)
    return [{"id": sig.id, "summary": sig.summary, "score": score}]


# ---------------------------------------------------------------------------
# Novelty scoring via LLM
# ---------------------------------------------------------------------------

_NOVELTY_SYSTEM = """You are a novelty evaluator for a technical founder's social media agent.
Given a commit message or milestone update from this project (an algorithmic trading AI platform),
rate its novelty and potential interest to a technical audience on a scale of 0.0 to 1.0.

Score 0.8+ for: open-source releases, production launches, novel architectures, measurable results
Score 0.6-0.8 for: meaningful features, interesting technical decisions, milestone completions
Score below 0.6 for: chores, minor fixes, version bumps, docs typos

Respond with JSON only: {"score": 0.0-1.0, "summary": "one sentence for the post topic"}"""


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(
        (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.APIConnectionError)
    ),
)
async def _call_novelty_llm(mc, text: str) -> str:
    resp = await litellm.acompletion(
        model=mc.model,
        messages=[
            {"role": "system", "content": _NOVELTY_SYSTEM},
            {"role": "user", "content": text},
        ],
        response_format={"type": "json_object"},
        # Gemini 2.5 Flash spends its first ~800-1000 tokens on internal
        # "thinking" before emitting output. 2048 is the first value that
        # consistently returns a complete JSON object.
        max_tokens=2048,
        **mc.kwargs,
    )
    return resp.choices[0].message.content or "{}"


async def _score_novelty(text: str, source_type: str) -> tuple[float, str]:
    if settings().is_dry_run:
        return 0.75, f"[dry-run] {text[:80]}"

    mc = pick("cheap")
    try:
        raw = await _call_novelty_llm(mc, text)
        data = json.loads(raw)
        score = float(data.get("score", 0.0))
        summary = str(data.get("summary", text[:80]))
        return score, summary
    except Exception as exc:
        log.warning("scout.novelty_score_failed", error=str(exc)[:200])
        return 0.0, ""
