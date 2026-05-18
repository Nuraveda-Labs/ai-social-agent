"""Drive-footage content source — scout, caption writer, graph routing."""
from __future__ import annotations

import os
import pathlib
from datetime import UTC

import pytest

os.environ.setdefault("DISPATCH_MODE", "dry_run")
os.environ.setdefault("SIGNAL_DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN_SIGNAL", "0:test")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_API_KEY", "test")
os.environ.setdefault("AUTH_ENCRYPTION_KEY", "l3mgT3MDKZ2g8oh2l8r4e1XaS0o7Q8mT9H5V1v3P2Hk=")


@pytest.fixture(autouse=True)
def _reset_caches():
    from glitch_signal import config as cfg
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()
    yield
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()


# ---------------------------------------------------------------------------
# Graph routing
# ---------------------------------------------------------------------------

class TestGraphEntryRouting:
    def test_drive_footage_routes_to_drive_scout(self):
        from glitch_signal.agent.graph import _entry_router
        assert _entry_router({"content_source": "drive_footage"}) == "drive_scout"

    def test_ai_generated_routes_to_scout(self):
        from glitch_signal.agent.graph import _entry_router
        assert _entry_router({"content_source": "ai_generated"}) == "scout"

    def test_missing_content_source_defaults_to_scout(self):
        from glitch_signal.agent.graph import _entry_router
        assert _entry_router({}) == "scout"
        assert _entry_router({"content_source": ""}) == "scout"

    def test_case_insensitive(self):
        from glitch_signal.agent.graph import _entry_router
        assert _entry_router({"content_source": "DRIVE_FOOTAGE"}) == "drive_scout"


# ---------------------------------------------------------------------------
# Google Drive file filtering
# ---------------------------------------------------------------------------

class TestDriveFileFiltering:
    def test_is_video_by_mime(self):
        from glitch_signal.integrations.google_drive import _is_video
        assert _is_video("clip.mp4", "video/mp4")
        assert _is_video("clip.webm", "video/webm")
        assert not _is_video("doc.pdf", "application/pdf")

    def test_is_video_by_extension_when_mime_missing(self):
        from glitch_signal.integrations.google_drive import _is_video
        # Some Drive exports don't set mimeType reliably; extension fallback matters.
        assert _is_video("clip.mp4", "")
        assert _is_video("clip.MOV", "application/octet-stream")
        assert not _is_video("spreadsheet.xlsx", "application/octet-stream")

    def test_normalise_filters_non_video(self):
        from glitch_signal.integrations.google_drive import _normalise
        rec = _normalise(
            {"id": "f1", "name": "notes.pdf", "mimeType": "application/pdf"},
            svc=None,
        )
        assert rec is None


# ---------------------------------------------------------------------------
# drive_scout — mocked Drive client
# ---------------------------------------------------------------------------

class TestDriveScoutNode:
    @pytest.mark.asyncio
    async def test_happy_path_creates_signals_without_downloading(self, tmp_path, monkeypatch):
        from glitch_signal.integrations.google_drive import DriveFile

        # Brand config fixture: drive_footage + known folder id
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "glitch_executor.json").write_text(_minimal_brand_json("glitch_executor"))
        (configs / "drive_brand.json").write_text(_minimal_brand_json(
            "drive_brand",
            content_source="drive_footage",
            drive_folder_id="FOLDER_XYZ",
        ))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "glitch_executor")
        monkeypatch.setenv("VIDEO_STORAGE_PATH", str(tmp_path / "videos"))

        from glitch_signal import config as cfg
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        # Mock Drive client methods so no network touches happen.
        import glitch_signal.integrations.google_drive as gdrive

        fake_files = [
            DriveFile(id="F1", name="clip_a.mp4", mime_type="video/mp4", size=1000, md5=None, modified_time=None),
            DriveFile(id="F2", name="clip_b.mov", mime_type="video/quicktime", size=2000, md5=None, modified_time=None),
        ]

        async def fake_list(folder_id):
            assert folder_id == "FOLDER_XYZ"
            return fake_files

        async def must_not_download(*a, **kw):
            raise AssertionError(
                "drive_scout is JIT now — publisher downloads at post time, "
                "scout only records metadata"
            )

        monkeypatch.setattr(gdrive, "list_video_files", fake_list)
        monkeypatch.setattr(gdrive, "download_file", must_not_download)

        # In-memory SQLite for DB writes.
        await _make_memory_db()

        from glitch_signal.agent.nodes.drive_scout import drive_scout_node
        state = await drive_scout_node({"brand_id": "drive_brand"})

        assert state.get("error") is None, state.get("error")
        assert state["brand_id"] == "drive_brand"
        assert len(state["signals"]) == 2
        assert state["signal_id"]  # first new signal promoted
        assert state["platform"] == "tiktok"

        # Expected local paths ARE recorded on the state but files are
        # NOT downloaded — publisher will JIT-download at post time.
        expected_paths = [s["local_path"] for s in state["signals"]]
        assert (tmp_path / "videos" / "drive" / "drive_brand" / "F1.mp4").as_posix() in [
            pathlib.Path(p).as_posix() for p in expected_paths
        ]
        assert not (tmp_path / "videos" / "drive" / "drive_brand" / "F1.mp4").exists()
        assert not (tmp_path / "videos" / "drive" / "drive_brand" / "F2.mov").exists()

        # Re-running yields zero new signals (dedup by source_ref)
        state2 = await drive_scout_node({"brand_id": "drive_brand"})
        assert state2["signals"] == []

    @pytest.mark.asyncio
    async def test_rejects_wrong_content_source(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "glitch_executor.json").write_text(_minimal_brand_json("glitch_executor"))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "glitch_executor")

        from glitch_signal import config as cfg
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        from glitch_signal.agent.nodes.drive_scout import drive_scout_node
        state = await drive_scout_node({"brand_id": "glitch_executor"})
        assert "expected 'drive_footage'" in (state.get("error") or "")


# ---------------------------------------------------------------------------
# caption_writer — writes ContentScript + VideoAsset regardless of DISPATCH_MODE.
# The LLM call itself is mocked here so we don't hit the wire.
# ---------------------------------------------------------------------------

class TestCaptionWriterNode:
    @pytest.mark.asyncio
    async def test_writes_content_script_and_asset(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "drive_brand.json").write_text(_minimal_brand_json(
            "drive_brand",
            content_source="drive_footage",
            drive_folder_id="FOLDER_XYZ",
            default_hashtags=["#tag1", "#brand"],
        ))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "drive_brand")
        monkeypatch.setenv("VIDEO_STORAGE_PATH", str(tmp_path / "videos"))

        from glitch_signal import config as cfg
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        factory = await _make_memory_db()

        # Seed a Signal row to caption.
        import uuid
        from datetime import datetime

        from glitch_signal.db.models import Signal

        sig_id = str(uuid.uuid4())
        async with factory() as session:
            session.add(Signal(
                id=sig_id,
                brand_id="drive_brand",
                source="drive",
                source_ref="F1",
                summary="Drive clip: clip_a.mp4",
                novelty_score=1.0,
                status="queued",
                created_at=datetime.now(UTC).replace(tzinfo=None),
            ))
            await session.commit()

        # Drop a fake local file so _probe_duration has something (it'll just return 0 without ffmpeg — fine).
        local = tmp_path / "videos" / "drive" / "drive_brand" / "F1.mp4"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"mock")

        # Mock the LLM call so caption_writer doesn't try to hit Gemini.
        from unittest.mock import AsyncMock, patch
        mock_resp = type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": '{"title": "Golden hour root", "caption": "A moment with the root.\\n\\n#tag1 #brand", "hashtags": ["tag1", "brand"]}'})()})()]})()

        from glitch_signal.agent.nodes.caption_writer import caption_writer_node
        with patch("glitch_signal.agent.nodes.caption_writer.litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            state = await caption_writer_node({
                "brand_id": "drive_brand",
                "signal_id": sig_id,
                "platform": "tiktok",
                "signals": [{"id": sig_id, "local_path": str(local)}],
            })

        assert state.get("error") is None, state.get("error")
        assert state["script_id"]
        assert state["asset_id"]
        # LLM output flows through, not a dry-run fallback.
        assert "A moment with the root." in state["script_body"]
        assert "#tag1" in state["script_body"]

        # Confirm both rows landed with the right brand_id
        from sqlmodel import select

        from glitch_signal.db.models import ContentScript, VideoAsset
        async with factory() as session:
            cs_rows = (await session.execute(select(ContentScript))).scalars().all()
            va_rows = (await session.execute(select(VideoAsset))).scalars().all()
        assert len(cs_rows) == 1 and cs_rows[0].brand_id == "drive_brand"
        assert len(va_rows) == 1 and va_rows[0].brand_id == "drive_brand"
        assert va_rows[0].assembler_version.startswith("drive_passthrough")


# ---------------------------------------------------------------------------
# caption_writer — LLM output parsing robustness
# ---------------------------------------------------------------------------

class TestCaptionJsonParser:
    def test_plain_json(self):
        from glitch_signal.agent.nodes.caption_writer import _parse_caption_json
        got = _parse_caption_json('{"title": "a", "caption": "b", "hashtags": ["x"]}')
        assert got == {"title": "a", "caption": "b", "hashtags": ["x"]}

    def test_markdown_fenced(self):
        from glitch_signal.agent.nodes.caption_writer import _parse_caption_json
        got = _parse_caption_json('```json\n{"title": "a", "caption": "b"}\n```')
        assert got == {"title": "a", "caption": "b"}

    def test_truncated_recovers_prefix(self):
        from glitch_signal.agent.nodes.caption_writer import _parse_caption_json
        # Model ran out of tokens mid-generation: everything before the
        # last complete brace should still be usable.
        truncated = '{"title": "a", "caption": "b"} and then trailing gar'
        got = _parse_caption_json(truncated)
        assert got.get("caption") == "b"

    def test_empty_returns_empty(self):
        from glitch_signal.agent.nodes.caption_writer import _parse_caption_json
        assert _parse_caption_json("") == {}
        assert _parse_caption_json("   ") == {}

    def test_garbage_returns_empty(self):
        from glitch_signal.agent.nodes.caption_writer import _parse_caption_json
        assert _parse_caption_json("not json at all, no braces") == {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_brand_json(brand_id: str, **overrides) -> str:
    import json
    base = {
        "brand_id": brand_id,
        "display_name": brand_id.replace("_", " ").title(),
        "timezone": "UTC",
        "content_source": "ai_generated",
        "orm_guardrails": {
            "hard_stop_phrases": [],
            "competitor_names": [],
            "auto_respond_tiers": ["positive"],
            "review_window_seconds": {"negative_mild": 7200},
            "escalate_tiers": ["negative_severe"],
            "ignore_tiers": ["spam"],
            "min_confidence_threshold": 0.7,
        },
        "platforms": {},
        "default_hashtags": [],
    }
    base.update(overrides)
    return json.dumps(base)


async def _make_memory_db(monkeypatch=None):
    """Build an in-memory SQLite DB and rewire every node's _session_factory.

    Each node imports _session_factory as a bare name at module load, so we
    have to patch the binding in the *importing* module's namespace — not
    just in db.session.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import SQLModel
    from sqlmodel.ext.asyncio.session import AsyncSession

    import glitch_signal.agent.nodes.caption_writer as cw_mod
    import glitch_signal.agent.nodes.drive_scout as ds_mod
    import glitch_signal.db.models  # noqa: F401 — register metadata
    import glitch_signal.db.session as dbs

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    def _factory_getter():
        return factory

    for mod in (dbs, ds_mod, cw_mod):
        mod._session_factory = _factory_getter

    return factory
