"""Vision-mode caption writer — mode switching + Gemini File API integration."""
from __future__ import annotations

import json
import os
import pathlib
import sys

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


def _write_brand(
    configs_dir: pathlib.Path,
    brand_id: str,
    *,
    mode: str = "filename",
    vision_fallback: bool = True,
) -> None:
    cfg = {
        "brand_id": brand_id,
        "display_name": f"{brand_id.title()} Brand",
        "timezone": "UTC",
        "platforms": {},
        "caption_writer": {
            "mode": mode,
            "vision_fallback_to_filename": vision_fallback,
        },
        "default_hashtags": ["#tag1", "#tag2"],
    }
    (configs_dir / f"{brand_id}.json").write_text(json.dumps(cfg))


class _FakeSignal:
    """Mimics the Signal SQLModel attributes caption_writer reads."""

    def __init__(self, summary: str, signal_id: str = "sig-test"):
        self.id = signal_id
        self.summary = summary


class TestModeRouting:
    """Pass one call to the expected path; the other path must not run."""

    @pytest.mark.asyncio
    async def test_filename_mode_skips_vision(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.agent.nodes import caption_writer as cw

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "film", mode="filename")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "film")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")

        async def fake_filename(*, system_prompt, user_context):
            return {"title": "T", "caption": "C #tag1", "hashtags": ["tag1"]}

        async def no_vision(**kwargs):
            raise AssertionError("vision path must NOT run in filename mode")

        monkeypatch.setattr(cw, "_generate_via_filename", fake_filename)
        monkeypatch.setattr(cw, "_generate_via_vision", no_vision)

        title, caption, tags = await cw._generate_caption(
            _FakeSignal("clip.mp4"), "film", "tiktok", local_path=vid
        )
        assert title == "T"
        assert caption == "C #tag1"
        assert tags == ["tag1"]

    @pytest.mark.asyncio
    async def test_vision_mode_runs_vision_path(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.agent.nodes import caption_writer as cw

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "vsn", mode="vision")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "vsn")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"video-bytes")

        captured = {}

        async def fake_vision(**kwargs):
            captured.update(kwargs)
            return {
                "title": "Brahmi hair oil demo",
                "caption": "Slow scalp massage with brahmi oil #ayurveda #haircare",
                "hashtags": ["ayurveda", "haircare"],
            }

        async def no_filename(**kwargs):
            raise AssertionError("filename path must NOT run when vision succeeds")

        monkeypatch.setattr(cw, "_generate_via_vision", fake_vision)
        monkeypatch.setattr(cw, "_generate_via_filename", no_filename)

        title, caption, tags = await cw._generate_caption(
            _FakeSignal("some_filename.mp4"), "vsn", "tiktok", local_path=vid
        )
        assert title == "Brahmi hair oil demo"
        assert "brahmi" in caption.lower()
        assert tags == ["ayurveda", "haircare"]
        # Confirm the actual file path reached the vision path.
        assert captured["local_path"] == vid

    @pytest.mark.asyncio
    async def test_vision_mode_with_missing_file_falls_back_to_filename(
        self, tmp_path, monkeypatch
    ):
        """If the local file somehow isn't on disk, vision path is skipped and
        we use filename mode rather than raising."""
        from glitch_signal import config as cfg
        from glitch_signal.agent.nodes import caption_writer as cw

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "vsn2", mode="vision")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "vsn2")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        absent = tmp_path / "does_not_exist.mp4"

        async def no_vision(**kwargs):
            raise AssertionError("vision must not run when file is missing")

        async def fake_filename(*, system_prompt, user_context):
            return {"title": "Fallback", "caption": "Fallback body", "hashtags": []}

        monkeypatch.setattr(cw, "_generate_via_vision", no_vision)
        monkeypatch.setattr(cw, "_generate_via_filename", fake_filename)

        title, caption, _ = await cw._generate_caption(
            _FakeSignal("ghost"), "vsn2", "tiktok", local_path=absent
        )
        assert title == "Fallback"
        assert caption == "Fallback body"


class TestVisionFallback:
    @pytest.mark.asyncio
    async def test_vision_exception_falls_back_to_filename(
        self, tmp_path, monkeypatch
    ):
        from glitch_signal import config as cfg
        from glitch_signal.agent.nodes import caption_writer as cw

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "vsn3", mode="vision", vision_fallback=True)
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "vsn3")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")

        async def boom(**kwargs):
            raise RuntimeError("gemini quota exhausted")

        async def fake_filename(*, system_prompt, user_context):
            return {"title": "T", "caption": "C", "hashtags": []}

        monkeypatch.setattr(cw, "_generate_via_vision", boom)
        monkeypatch.setattr(cw, "_generate_via_filename", fake_filename)

        title, caption, _ = await cw._generate_caption(
            _FakeSignal("x"), "vsn3", "tiktok", local_path=vid
        )
        assert title == "T"
        assert caption == "C"

    @pytest.mark.asyncio
    async def test_vision_exception_propagates_when_fallback_disabled(
        self, tmp_path, monkeypatch
    ):
        from glitch_signal import config as cfg
        from glitch_signal.agent.nodes import caption_writer as cw

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "vsn4", mode="vision", vision_fallback=False)
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "vsn4")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"x")

        async def boom(**kwargs):
            raise RuntimeError("gemini quota exhausted")

        async def must_not_run(**kwargs):
            raise AssertionError("filename fallback must not run when disabled")

        monkeypatch.setattr(cw, "_generate_via_vision", boom)
        monkeypatch.setattr(cw, "_generate_via_filename", must_not_run)

        with pytest.raises(RuntimeError, match="quota exhausted"):
            await cw._generate_caption(
                _FakeSignal("x"), "vsn4", "tiktok", local_path=vid
            )


class TestGenerateViaVisionSDK:
    """Exercise the actual _generate_via_vision implementation against a
    fake google-genai module — verifies upload → wait → generate → delete."""

    def _install_fake_genai(
        self,
        monkeypatch,
        *,
        processing_ticks: int = 0,
        final_state: str = "ACTIVE",
        generate_text: str = '{"title":"V","caption":"Vision body","hashtags":["a"]}',
    ):
        call_log: list[str] = []

        class _File:
            def __init__(self, name, state):
                self.name = name
                self.state = state

        class _Files:
            def __init__(self):
                self.remaining = processing_ticks

            def upload(self, *, file):
                call_log.append(f"upload:{file}")
                state = "PROCESSING" if self.remaining > 0 else final_state
                return _File("files/abc", state)

            def get(self, *, name):
                call_log.append(f"get:{name}")
                self.remaining -= 1
                state = "PROCESSING" if self.remaining > 0 else final_state
                return _File(name, state)

            def delete(self, *, name):
                call_log.append(f"delete:{name}")

        class _GenResp:
            text = generate_text

        class _Models:
            def generate_content(self, *, model, contents, config):
                call_log.append(f"generate:{model}:{len(contents)}")
                return _GenResp()

        class _Client:
            def __init__(self, *, api_key=None):
                self.files = _Files()
                self.models = _Models()

        class _Types:
            class GenerateContentConfig:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

        fake_genai = type("M", (), {"Client": _Client})
        fake_types = _Types

        # Replace the `from google import genai` lookup.
        fake_google = type("M", (), {"genai": fake_genai})
        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
        # google.genai submodule attribute for `from google.genai import types`
        fake_genai.types = fake_types

        return call_log

    @pytest.mark.asyncio
    async def test_active_on_first_check_happy_path(self, tmp_path, monkeypatch):
        from glitch_signal.agent.nodes import caption_writer as cw

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"xyz")
        calls = self._install_fake_genai(monkeypatch)

        data = await cw._generate_via_vision(
            local_path=vid,
            system_prompt="S",
            user_context="U",
        )
        assert data == {"title": "V", "caption": "Vision body", "hashtags": ["a"]}
        # Verify full lifecycle: upload, generate, delete (no polling needed).
        assert any(c.startswith("upload:") for c in calls)
        assert any(c.startswith("generate:gemini-2.5-pro:") for c in calls)
        assert any(c.startswith("delete:files/abc") for c in calls)

    @pytest.mark.asyncio
    async def test_processing_then_active_polls(self, tmp_path, monkeypatch):
        """Upload returns PROCESSING once → we should poll get until ACTIVE."""
        from glitch_signal.agent.nodes import caption_writer as cw

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"xyz")
        calls = self._install_fake_genai(monkeypatch, processing_ticks=2)

        # Short-circuit sleep so the test isn't flaky on slow CI.
        import time
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        data = await cw._generate_via_vision(
            local_path=vid, system_prompt="S", user_context="U",
        )
        assert data["title"] == "V"
        # At least one get call was made during processing.
        assert any(c.startswith("get:") for c in calls)

    @pytest.mark.asyncio
    async def test_file_api_failed_raises(self, tmp_path, monkeypatch):
        from glitch_signal.agent.nodes import caption_writer as cw

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"xyz")
        self._install_fake_genai(monkeypatch, final_state="FAILED")

        with pytest.raises(RuntimeError, match="processing failed"):
            await cw._generate_via_vision(
                local_path=vid, system_prompt="S", user_context="U",
            )

    @pytest.mark.asyncio
    async def test_model_override_is_passed(self, tmp_path, monkeypatch):
        from glitch_signal.agent.nodes import caption_writer as cw

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"xyz")
        calls = self._install_fake_genai(monkeypatch)

        await cw._generate_via_vision(
            local_path=vid, system_prompt="S", user_context="U",
            model_override="gemini-2.5-pro-latest",
        )
        assert any("generate:gemini-2.5-pro-latest:" in c for c in calls)

    @pytest.mark.asyncio
    async def test_cleanup_still_called_after_generate_raises(
        self, tmp_path, monkeypatch
    ):
        """If generate_content raises, files.delete must still run so we don't
        pile up orphan uploads on Gemini's side."""
        from glitch_signal.agent.nodes import caption_writer as cw

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"xyz")
        calls = self._install_fake_genai(monkeypatch)

        # Patch the fake client so generate raises.
        import google.genai as fake_genai

        class _BadModels:
            def generate_content(self, **_):
                raise RuntimeError("gemini 500")

        orig_client = fake_genai.Client

        class _WrappedClient(orig_client):  # type: ignore[misc, valid-type]
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.models = _BadModels()

        fake_genai.Client = _WrappedClient

        try:
            with pytest.raises(RuntimeError, match="gemini 500"):
                await cw._generate_via_vision(
                    local_path=vid, system_prompt="S", user_context="U",
                )
        finally:
            fake_genai.Client = orig_client

        assert any(c.startswith("delete:files/abc") for c in calls)

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.agent.nodes import caption_writer as cw

        monkeypatch.setenv("GOOGLE_API_KEY", "")
        cfg.settings.cache_clear()

        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"xyz")

        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            await cw._generate_via_vision(
                local_path=vid, system_prompt="S", user_context="U",
            )
