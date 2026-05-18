"""Filename parser — coverage against the real client Drive filenames."""
from __future__ import annotations

import pytest

from glitch_signal.media.filename_parser import parse


class TestProductExtraction:
    @pytest.mark.parametrize("name,expected_product", [
        # Underscored + canonical
        ("Liver_ad15_Editor3_02_04_26.mp4",        "liver"),
        ("Lungs_UK_voiceover_var1_editor1_31/3/26.mp4", "lungs"),
        ("Thyroid_ad7_editor1_8/4/26.mp4",          "thyroid"),
        ("Fatloss_ad2_var3_Editor3_02_04_26.mp4",  "fatloss"),
        # Space-separated + trailing dup marker
        ("LIver 6 (1)_Editor3_02.04.26.mp4",       "liver"),
        ("Liver 13 UK Editor3 02_04_2026-.mp4",    "liver"),
        # Product-glued-to-number ("thyroid9", "wht2", "liver6")
        ("thyroid9_var1_headings4_editor1_2.4.2026.mp4", "thyroid"),
        ("wht2_v1_uae heading 1_editor2_10.4.26.mp4", "wht"),
        ("liver6_uk female_changed1_editor2_1.4.26.mp4", "liver"),
        # Misspelling alias
        ("diabetis 3 v1_ Editor3_02.04.26.mp4",    "diabetes"),
        # Slash-bearing date in filename
        ("Uk_liver_ad15_h1_9.4.26.mp4",            "liver"),
        ("Lungs_UK_voiceover_var9_editor1_2/4/26",  "lungs"),
    ])
    def test_product_extraction(self, name, expected_product):
        assert parse(name).product == expected_product

    def test_multi_mention_takes_first_priority(self):
        # "Lungs_ad2" should pick "lungs" not something else.
        p = parse("lungs_ad2_uk male voice_20.03.26.mp4")
        assert p.product == "lungs"
        assert p.ad_num == 2


class TestAdNumExtraction:
    @pytest.mark.parametrize("name,expected", [
        ("Liver_ad15_Editor3_02_04_26.mp4",   15),
        ("Uk_liver_ad20_h5_9.4.26.mp4",       20),
        ("Uk_liver_ad40_h9_9.4.26.mp4",       40),
        ("LIver 6 (1)_Editor3_02.04.26.mp4",  6),
        ("thyroid9_var1_headings4_editor1_2.4.2026.mp4", 9),
        ("diabetis 3 v1_ Editor3_02.04.26.mp4", 3),
    ])
    def test_ad_num(self, name, expected):
        assert parse(name).ad_num == expected


class TestGeoExtraction:
    @pytest.mark.parametrize("name,expected", [
        ("Uk_liver_ad15_h1_9.4.26.mp4",      "uk"),
        ("UAE_Wht_ad1_var1_10/4/26.mp4",     "uae"),
        ("Liver_ad15_Editor3_02_04_26.mp4",  None),   # no geo → None
    ])
    def test_geo(self, name, expected):
        assert parse(name).geo == expected


class TestVariantGroup:
    """variant_group is the rotation key — its stability is critical."""

    def test_same_creative_different_headings_share_group(self):
        g1 = parse("Uk_liver_ad15_h1_9.4.26.mp4").variant_group
        g2 = parse("Uk_liver_ad15_h4_9.4.26.mp4").variant_group
        assert g1 == g2 == "liver_ad15_uk"

    def test_same_product_different_ad_not_grouped(self):
        g15 = parse("Uk_liver_ad15_h1_9.4.26.mp4").variant_group
        g20 = parse("Uk_liver_ad20_h5_9.4.26.mp4").variant_group
        assert g15 != g20

    def test_different_geo_not_grouped(self):
        uk = parse("Uk_liver_ad15_h1_9.4.26.mp4").variant_group
        uae = parse("Liver_ad2_UAE_var 2_editor1_10/4/26.mp4").variant_group
        assert uk != uae

    def test_product_glued_to_number(self):
        g = parse("thyroid9_var1_headings4_editor1_2.4.2026.mp4").variant_group
        assert g == "thyroid_ad9"

    def test_fully_unparseable_goes_to_unknown(self):
        g = parse("random_filename_no_pattern.mp4").variant_group
        assert g == "unknown"


class TestVariantTags:
    """Variant tags are descriptive breadcrumbs — exact set matters less
    than each expected breadcrumb being present and normalised."""

    def test_picks_up_var_and_h_tags(self):
        tags = parse("Lungs_UK_voiceover_var3_editor1_31/3/26.mp4").variant_tags
        assert "var3" in tags
        assert "voiceover" in tags

    def test_picks_up_headings(self):
        p = parse("lungs_ad1_speedup_headings7_2.4.2026.mp4")
        assert "headings7" in p.variant_tags
        assert "speedup" in p.variant_tags

    def test_editor_extracted_when_editors_list_supplied(self):
        """Editor names are not compiled into the repo — callers inject a
        brand-specific list via the `editors` kwarg."""
        p = parse("lungs_ad1_headings7_alice_2.4.2026.mp4", editors=("alice", "bob"))
        assert p.editor == "alice"

    def test_no_editor_match_when_list_empty(self):
        p = parse("lungs_ad1_headings7_someone_2.4.2026.mp4")
        assert p.editor is None

    def test_spaces_in_tag_normalised(self):
        p = parse("Liver_ad2_UAE_var 2_editor1_10/4/26.mp4")
        assert "var2" in p.variant_tags
