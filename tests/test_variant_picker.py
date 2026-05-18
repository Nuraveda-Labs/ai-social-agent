"""Variant-aware scheduler picker — gating rules + anti-repeat logic.

Exercises the pure-function surface of scheduler/queue.py without
spinning up a real scheduler tick:

- _in_any_slot         — slot-window gating
- _first_eligible      — variant/product gap + skip-pattern rejection
- _pick_with_rules     — integration via fakes (covered in smoke)
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

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


class _FakeSP:
    """Stand-in for ScheduledPost with only the fields the picker reads."""
    def __init__(self, sp_id, variant_group=None, product=None, scheduled_for=None):
        self.id = sp_id
        self.variant_group = variant_group
        self.product = product
        self.scheduled_for = scheduled_for or datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Slot windows
# ---------------------------------------------------------------------------

class TestInAnySlot:
    def test_exact_slot_match(self):
        from glitch_signal.scheduler.queue import _in_any_slot
        now = datetime(2026, 4, 17, 17, 30)
        assert _in_any_slot(now, ["17:30", "22:00"], tolerance_minutes=15)

    def test_within_tolerance(self):
        from glitch_signal.scheduler.queue import _in_any_slot
        now = datetime(2026, 4, 17, 17, 40)
        assert _in_any_slot(now, ["17:30", "22:00"], tolerance_minutes=15)

    def test_outside_every_slot(self):
        from glitch_signal.scheduler.queue import _in_any_slot
        now = datetime(2026, 4, 17, 14, 0)
        assert not _in_any_slot(now, ["17:30", "22:00"], tolerance_minutes=15)

    def test_bad_slot_string_ignored(self):
        from glitch_signal.scheduler.queue import _in_any_slot
        now = datetime(2026, 4, 17, 22, 0)
        # "garbage" is ignored; "22:00" still matches.
        assert _in_any_slot(now, ["garbage", "22:00"], tolerance_minutes=5)


# ---------------------------------------------------------------------------
# _first_eligible — the heart of the anti-repeat logic
# ---------------------------------------------------------------------------

class TestFirstEligible:
    def test_picks_oldest_when_no_recent_history(self):
        from glitch_signal.scheduler.queue import _first_eligible
        a = _FakeSP("a", variant_group="liver_ad15_uk", product="liver")
        b = _FakeSP("b", variant_group="lungs_uk", product="lungs")
        pick = _first_eligible(
            [a, b],
            recent_variant_groups=[], recent_products=[],
            variant_gap=5, product_gap=2, skip_patterns=[],
        )
        assert pick is a

    def test_skips_candidate_in_recent_variant_window(self):
        from glitch_signal.scheduler.queue import _first_eligible
        # Most recent post was liver_ad15_uk → variant_gap=5 means the
        # next 5 picks cannot repeat that group.
        dupe = _FakeSP("dupe", variant_group="liver_ad15_uk", product="liver")
        fresh = _FakeSP("fresh", variant_group="lungs_uk",   product="lungs")
        pick = _first_eligible(
            [dupe, fresh],
            recent_variant_groups=["liver_ad15_uk"], recent_products=[],
            variant_gap=5, product_gap=0, skip_patterns=[],
        )
        assert pick is fresh

    def test_skips_candidate_with_recent_product(self):
        """product_gap=2 blocks repeat products in the last 2 posts."""
        from glitch_signal.scheduler.queue import _first_eligible
        liver2 = _FakeSP("x", variant_group="liver_ad40_uk", product="liver")
        lungs  = _FakeSP("y", variant_group="lungs_uk",      product="lungs")
        pick = _first_eligible(
            [liver2, lungs],
            recent_variant_groups=[], recent_products=["liver"],
            variant_gap=0, product_gap=2, skip_patterns=[],
        )
        assert pick is lungs

    def test_variant_gap_zero_disables_anti_repeat(self):
        from glitch_signal.scheduler.queue import _first_eligible
        dupe = _FakeSP("d", variant_group="liver_ad15_uk", product="liver")
        pick = _first_eligible(
            [dupe],
            recent_variant_groups=["liver_ad15_uk"], recent_products=[],
            variant_gap=0, product_gap=0, skip_patterns=[],
        )
        assert pick is dupe

    def test_skip_pattern_excludes(self):
        from glitch_signal.scheduler.queue import _first_eligible
        draft = _FakeSP("x", variant_group="liver_draft_uk", product="liver")
        prod  = _FakeSP("y", variant_group="liver_ad20_uk",  product="liver")
        pick = _first_eligible(
            [draft, prod],
            recent_variant_groups=[], recent_products=[],
            variant_gap=0, product_gap=0, skip_patterns=["draft"],
        )
        assert pick is prod

    def test_returns_none_when_nothing_eligible(self):
        from glitch_signal.scheduler.queue import _first_eligible
        only = _FakeSP("only", variant_group="liver_ad15_uk", product="liver")
        assert _first_eligible(
            [only],
            recent_variant_groups=["liver_ad15_uk"], recent_products=[],
            variant_gap=5, product_gap=0, skip_patterns=[],
        ) is None


# ---------------------------------------------------------------------------
# Posting-rules resolution from brand config
# ---------------------------------------------------------------------------

class TestPostingRulesFor:
    def test_returns_none_when_task_disabled(self, tmp_path, monkeypatch):
        import json

        from glitch_signal import config as cfg
        from glitch_signal.scheduler.queue import _posting_rules_for

        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "brand_a.json").write_text(json.dumps({
            "brand_id": "brand_a", "display_name": "A", "timezone": "UTC",
            "platforms": {},
            "tasks": {"video_uploader": {"enabled": False, "posting_rules": {"daily_cap": 1}}},
        }))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_a")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert _posting_rules_for("brand_a") is None

    def test_returns_rules_when_enabled(self, tmp_path, monkeypatch):
        import json

        from glitch_signal import config as cfg
        from glitch_signal.scheduler.queue import _posting_rules_for

        configs = tmp_path / "configs"
        configs.mkdir()
        rules = {"daily_cap": 2, "slots_local": ["17:30"]}
        (configs / "brand_b.json").write_text(json.dumps({
            "brand_id": "brand_b", "display_name": "B", "timezone": "Asia/Kolkata",
            "platforms": {},
            "tasks": {"video_uploader": {"enabled": True, "posting_rules": rules}},
        }))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_b")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert _posting_rules_for("brand_b") == rules

    def test_returns_none_when_no_tasks_block(self, tmp_path, monkeypatch):
        import json

        from glitch_signal import config as cfg
        from glitch_signal.scheduler.queue import _posting_rules_for

        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "brand_c.json").write_text(json.dumps({
            "brand_id": "brand_c", "display_name": "C", "timezone": "UTC",
            "platforms": {},
        }))
        monkeypatch.setenv("BRAND_CONFIGS_DIR", str(configs))
        monkeypatch.setenv("DEFAULT_BRAND_ID", "brand_c")
        cfg.settings.cache_clear()
        cfg._reset_brand_registry_for_tests()

        assert _posting_rules_for("brand_c") is None
