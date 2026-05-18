"""Sheet tracker — config resolution + delegation to google_sheets module.

We don't hit real Sheets API in CI. The google_sheets module is mocked
so we can assert on the (sheet_id, worksheet, columns, row) payload
without network.
"""
from __future__ import annotations

import json
import os

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


def _write_brand(configs_dir, brand_id, sheet_block=None):
    cfg = {
        "brand_id": brand_id,
        "display_name": brand_id,
        "timezone": "UTC",
        "platforms": {},
    }
    if sheet_block is not None:
        cfg["tasks"] = {
            "video_uploader": {
                "enabled": True,
                "outputs": {"google_sheet": sheet_block},
            }
        }
    (configs_dir / f"{brand_id}.json").write_text(json.dumps(cfg))


class TestSheetTarget:
    def test_returns_sheet_when_configured(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations.sheet_tracker import sheet_target

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b", {"sheet_id": "SHEET-123", "worksheet": "Posts"})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert sheet_target("b") == ("SHEET-123", "Posts")

    def test_default_worksheet(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations.sheet_tracker import sheet_target

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b", {"sheet_id": "SHEET-123"})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert sheet_target("b") == ("SHEET-123", "Sheet1")

    def test_returns_none_when_unconfigured(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations.sheet_tracker import sheet_target

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert sheet_target("b") is None


class TestAppendNewVideo:
    @pytest.mark.asyncio
    async def test_appends_row_with_parsed_fields(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations import sheet_tracker

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b", {"sheet_id": "SHEET-123", "worksheet": "Sheet1"})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        captured = {}
        async def fake_append(sheet_id, worksheet, columns, row):
            captured["sheet_id"] = sheet_id
            captured["worksheet"] = worksheet
            captured["columns"] = columns
            captured["row"] = row
        monkeypatch.setattr(
            "glitch_signal.integrations.sheet_tracker.gs.append_row", fake_append
        )

        await sheet_tracker.append_new_video(
            "b", video_name="Liver_ad15.mp4", drive_file_id="FID-1",
            product="liver", variant_group="liver_ad15_uk", geo="uk",
        )
        assert captured["sheet_id"] == "SHEET-123"
        assert captured["worksheet"] == "Sheet1"
        assert captured["row"]["video_name"] == "Liver_ad15.mp4"
        assert captured["row"]["drive_link"] == "https://drive.google.com/file/d/FID-1/view"
        assert captured["row"]["product"] == "liver"
        assert captured["row"]["variant_group"] == "liver_ad15_uk"
        assert captured["row"]["geo"] == "uk"
        assert captured["row"]["status"] == "queued"

    @pytest.mark.asyncio
    async def test_noop_when_sheet_not_configured(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations import sheet_tracker

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b")   # no sheet block
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        async def must_not_run(*a, **kw):
            raise AssertionError("append_row must not be called when no sheet configured")
        monkeypatch.setattr(
            "glitch_signal.integrations.sheet_tracker.gs.append_row", must_not_run
        )
        # Should complete silently.
        await sheet_tracker.append_new_video(
            "b", video_name="x.mp4", drive_file_id="id",
        )

    @pytest.mark.asyncio
    async def test_swallows_api_error(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations import sheet_tracker

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b", {"sheet_id": "SHEET-123"})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        async def boom(*a, **kw):
            raise RuntimeError("sheets 500")
        monkeypatch.setattr(
            "glitch_signal.integrations.sheet_tracker.gs.append_row", boom
        )
        # Must NOT raise — scout should never fail on sheets outage.
        await sheet_tracker.append_new_video(
            "b", video_name="x.mp4", drive_file_id="id",
        )


class TestUpdateByVideoName:
    @pytest.mark.asyncio
    async def test_delegates_to_update_row_by_key(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations import sheet_tracker

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b", {"sheet_id": "SHEET-123"})
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        captured = {}
        async def fake_update(sheet_id, worksheet, columns, *, key_column, key_value, updates):
            captured.update({
                "sheet_id": sheet_id, "worksheet": worksheet,
                "key_column": key_column, "key_value": key_value,
                "updates": updates,
            })
            return True
        monkeypatch.setattr(
            "glitch_signal.integrations.sheet_tracker.gs.update_row_by_key", fake_update
        )

        ok = await sheet_tracker.update_by_video_name(
            "b", "Liver_ad15.mp4",
            {"status": "posted", "tiktok_url": "https://tt/x"},
        )
        assert ok is True
        assert captured["key_column"] == "video_name"
        assert captured["key_value"] == "Liver_ad15.mp4"
        assert captured["updates"]["status"] == "posted"

    @pytest.mark.asyncio
    async def test_returns_false_when_no_sheet(self, tmp_path, monkeypatch):
        from glitch_signal import config as cfg
        from glitch_signal.integrations import sheet_tracker

        configs = tmp_path / "configs"
        configs.mkdir()
        _write_brand(configs, "b")
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        ok = await sheet_tracker.update_by_video_name("b", "x.mp4", {"status": "x"})
        assert ok is False


class TestColLetter:
    @pytest.mark.parametrize("n,letter", [
        (1, "A"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA"),
    ])
    def test_col_letter(self, n, letter):
        from glitch_signal.integrations.google_sheets import _col_letter
        assert _col_letter(n) == letter
