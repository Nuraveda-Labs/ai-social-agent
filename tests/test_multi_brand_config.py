"""Multi-brand config layer — discovery, validation, isolation."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

# Minimal env so config.Settings() can instantiate without a real .env.
os.environ.setdefault("DISPATCH_MODE", "dry_run")
os.environ.setdefault("SIGNAL_DB_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN_SIGNAL", "0:test")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "0")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_API_KEY", "test")


@pytest.fixture(autouse=True)
def _reset_registry():
    """Drop the cached brand registry AND the cached Settings before and
    after each test — otherwise monkeypatched env vars leak into subsequent
    tests via the lru_cache on settings().
    """
    from glitch_signal import config as cfg

    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()
    yield
    cfg._reset_brand_registry_for_tests()
    cfg.settings.cache_clear()


def _write_config(dir_: pathlib.Path, brand_id: str, **overrides) -> pathlib.Path:
    data = {
        "brand_id": brand_id,
        "display_name": f"Test {brand_id}",
        "timezone": "UTC",
        "content_source": "ai_generated",
        "orm_guardrails": {
            "hard_stop_phrases": [f"{brand_id}_stop"],
            "competitor_names": [],
            "min_confidence_threshold": 0.7,
        },
    }
    data.update(overrides)
    path = dir_ / f"{brand_id}.json"
    path.write_text(json.dumps(data))
    return path


class TestMultiBrandLoader:
    def test_discovers_multiple_brands(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        _write_config(configs, "glitch_executor")
        _write_config(configs, "drive_brand", content_source="drive_footage")

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "glitch_executor")

        from glitch_signal import config as cfg

        cfg.settings.cache_clear()  # pick up the new env
        cfg._reset_brand_registry_for_tests()

        ids = cfg.brand_ids()
        # brand_ids() returns alphabetically sorted
        assert ids == ["drive_brand", "glitch_executor"]

        assert cfg.brand_config("drive_brand")["content_source"] == "drive_footage"
        assert cfg.brand_config("glitch_executor")["content_source"] == "ai_generated"

        # Backward-compat: no-arg call returns the default brand's config.
        assert cfg.brand_config()["brand_id"] == "glitch_executor"

    def test_filename_stem_must_match_brand_id(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        # Write a file whose internal brand_id disagrees with the filename.
        (configs / "drive_brand.json").write_text(
            json.dumps({
                "brand_id": "something_else",
                "display_name": "Mismatched",
                "timezone": "UTC",
            })
        )
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "something_else")

        from glitch_signal import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(RuntimeError, match="filename stem"):
            cfg.brand_ids()

    def test_missing_default_brand_fails(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        _write_config(configs, "drive_brand")

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "glitch_executor")

        from glitch_signal import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(RuntimeError, match="default_brand_id"):
            cfg.brand_ids()

    def test_unknown_brand_id_raises(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        _write_config(configs, "glitch_executor")

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "glitch_executor")

        from glitch_signal import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        with pytest.raises(KeyError, match="unknown_brand"):
            cfg.brand_config("unknown_brand")

    def test_fallback_when_no_configs_present(self, tmp_path, monkeypatch):
        # Empty dir, no legacy file — loader must fall back to defaults
        # (test-friendly) rather than crash at startup.
        configs = tmp_path / "configs"
        configs.mkdir()

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("BRAND_CONFIG_PATH", str(tmp_path / "nope.json"))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "glitch_executor")

        from glitch_signal import config as cfg

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        # Default brand must be resolvable.
        bc = cfg.brand_config()
        assert bc["brand_id"] == "glitch_executor"
        assert "hard_stop_phrases" in bc["orm_guardrails"]


class TestBrandScopedGuardrails:
    """Each brand's hard_stop_phrases must apply independently."""

    def test_guardrail_lookup_is_brand_scoped(self, tmp_path, monkeypatch):
        configs = tmp_path / "configs"
        configs.mkdir()
        _write_config(
            configs,
            "glitch_executor",
            orm_guardrails={
                "hard_stop_phrases": ["SEC"],
                "competitor_names": [],
                "min_confidence_threshold": 0.7,
            },
        )
        _write_config(
            configs,
            "drive_brand",
            orm_guardrails={
                "hard_stop_phrases": ["allergic reaction"],
                "competitor_names": [],
                "min_confidence_threshold": 0.7,
            },
        )

        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "glitch_executor")

        from glitch_signal import config as cfg
        from glitch_signal.orm import guardrails

        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        # "SEC" trips glitch_executor but NOT drive_brand
        safe_ge, _ = guardrails.check("breaking SEC rules", brand_id="glitch_executor")
        safe_nm, _ = guardrails.check("breaking SEC rules", brand_id="drive_brand")
        assert not safe_ge
        assert safe_nm

        # "allergic reaction" trips drive_brand but NOT glitch_executor
        safe_ge2, _ = guardrails.check("had an allergic reaction", brand_id="glitch_executor")
        safe_nm2, _ = guardrails.check("had an allergic reaction", brand_id="drive_brand")
        assert safe_ge2
        assert not safe_nm2


class TestModelsCarryBrandId:
    """SQLModel fields must include brand_id on every brand-scoped table."""

    def test_models_have_brand_id_field(self):
        from glitch_signal.db.models import (
            ContentScript,
            MentionEvent,
            MetricsSnapshot,
            OrmResponse,
            PublishedPost,
            ScheduledPost,
            ScoutCheckpoint,
            Signal,
            VideoAsset,
            VideoJob,
        )

        for model in (
            Signal,
            ContentScript,
            VideoJob,
            VideoAsset,
            ScheduledPost,
            PublishedPost,
            MetricsSnapshot,
            ScoutCheckpoint,
            MentionEvent,
            OrmResponse,
        ):
            assert "brand_id" in model.model_fields, (
                f"{model.__name__} missing brand_id field"
            )
