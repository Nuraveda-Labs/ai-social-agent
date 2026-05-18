"""Parse structured fields out of client-supplied Drive filenames.

The client Drive folder is seeded with Meta-ad variants — many files are
near-duplicates that share a base creative. We parse the filename to
pull out:

  - product      the topic (liver, lungs, thyroid, wht, diabetes, ...)
  - ad_num       the underlying Meta ad number (ad1, ad15, ad20, ...)
  - geo          target market (uk, uae, us)
  - variant_tag  the visible differentiator (var1, h1, headings2, v3, ...)
  - editor       the person who cut the file (brand-specific, from config)

A `variant_group` key is the join of product + ad_num + geo — all files
sharing that key are near-visual-duplicates of the same Meta ad, and
the scheduler uses it to space them out on the TikTok grid so a viewer
scrolling the profile doesn't see the same creative three times in a row.

Robust against real-world filename chaos observed on 2026-04-17:
  - Inconsistent casing: "LIver 6", "liver1", "Liver_ad15"
  - Delimiter mix: spaces, underscores, hyphens, dots
  - Date-slashes interleaved: "10/4/26", "2.4.2026"
  - Optional trailing "(1)" from Drive's dedup on manual re-upload
  - Extension variance: .mp4, no extension, double-extension
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_PRODUCTS = (
    # Order matters — longer / more specific keys first so "fatloss" wins
    # before a generic regex could grab "fat".
    #
    # --- Namhya (human Ayurveda) ---
    "fatloss",
    "periods",
    "menopause",
    "diabetes", "diabetis",         # common misspelling seen in filenames
    "thyroid",
    "liver",
    "lungs", "lung",
    "kidney",
    "wht",                          # women's health tea (PCOS/PCOD)
    "brahmi",
    "amla",
    "giloy", "guduchi",
    "shatavari",
    "haritaki", "harad",
    "saffron",
    "mushroom",
    "immunity",
    # --- example (pet supplements + chews) ---
    # Longer keys first to avoid prefix collisions.
    "hip-o-joint", "hipojoint", "hipjoint",
    "goodgut",
    "coolcalm", "cool-calm",
    "ashwagandha",                  # shared: example chew + Namhya powder
    "turmeric",                     # example chew + general Ayurvedic stem
    "hoj",                          # example filename code for Hip-O-Joint
    "gg",                           # example filename code for GoodGut
    "cc",                           # example filename code for CoolCalm
    "yak",                          # example yak cheese chews
    "joint",                        # example joint-support framing
    "arthritis",
    "mobility",
    "limping",
    "dental",                       # example yak chews angle
    "calming",                      # example calming chew / CoolCalm
    "anxiety",                      # CoolCalm framing
)

# Canonicalise product spellings observed to internal SKU keys.
_PRODUCT_ALIASES = {
    # Namhya
    "diabetis":     "diabetes",
    "lung":         "lungs",
    "guduchi":      "giloy",
    "harad":        "haritaki",
    # example — normalise short codes + variants to catalog names
    "hoj":          "hip-o-joint",
    "hipjoint":     "hip-o-joint",
    "hipojoint":    "hip-o-joint",
    "gg":           "goodgut",
    "cc":           "coolcalm",
    "cool-calm":    "coolcalm",
    # Framing keywords that map to the most-likely SKU so the rules-based
    # caption prompt gets product context. Not lossy — the original word
    # is preserved via `variant_tags` if the parser ever needs it.
    "arthritis":    "hip-o-joint",
    "mobility":     "hip-o-joint",
    "limping":      "hip-o-joint",
    "joint":        "hip-o-joint",
    "anxiety":      "coolcalm",
    "calming":      "coolcalm",
    "dental":       "yak",          # dental framing → Himalayan yak chews
}

_GEOS = ("uk", "uae", "usa", "us")

# Editor names are brand-specific and not committed. Brands with an
# editor-in-filename convention can inject their list at parse time by
# calling `parse(filename, editors=("...",))`. Leave empty for brands
# that don't tag editors or whose team names shouldn't live in repo.
_EDITORS: tuple[str, ...] = ()

# Variant-tag patterns. Each may appear zero or more times in a filename.
# We capture the full match (e.g. "var3", "h1", "headings2", "v1",
# "voiceover", "speedup", "changed1") and return all of them, so the
# scheduler can log what actually differentiated this file from its siblings.
_VARIANT_RE = re.compile(
    r"\b(var\s*\d+|v\d+|h\d+|heading[s]?\s*\d+|headline\s*\d+"
    r"|voiceover|speedup|changed\s*\d+|uk\s*female|uk\s*male\s*vo?)\b",
    re.IGNORECASE,
)

_AD_NUM_RE = re.compile(r"\bad\s*0*(\d+)\b", re.IGNORECASE)

# Dates in several separators: 2/4/26, 10/4/26, 02.04.26, 2.4.2026, 02_04_26.
_DATE_RE = re.compile(
    r"\b\d{1,2}[/._]\d{1,2}[/._]\d{2,4}\b",
)

# Drive's re-upload suffix — e.g. "foo (1).mp4".
_DRIVE_DUP_RE = re.compile(r"\s*\(\d+\)\s*$")


@dataclass(frozen=True)
class ParsedFilename:
    raw: str                   # original filename, unchanged
    stem: str                  # lowercased + normalized (dates/editors stripped)
    product: str | None        # canonical product key, e.g. "liver"
    ad_num: int | None         # ad number, e.g. 15
    geo: str | None            # "uk", "uae", "us", or None
    editor: str | None         # editor tag if matched against parse()'s `editors` arg
    variant_tags: tuple[str, ...]   # tuple to stay hashable
    variant_group: str         # composite key used for rotation

    def describe(self) -> str:
        parts = []
        if self.product:
            parts.append(self.product)
        if self.ad_num is not None:
            parts.append(f"ad{self.ad_num}")
        if self.geo:
            parts.append(self.geo.upper())
        if self.variant_tags:
            parts.append("/".join(self.variant_tags))
        return " · ".join(parts) or self.stem


def parse(filename: str, *, editors: tuple[str, ...] = _EDITORS) -> ParsedFilename:
    """Break a raw Drive filename into its structured pieces."""
    raw = filename
    # DON'T use pathlib.Path.stem — several client filenames contain
    # slashes inside the date ("..._2/4/26.mp4"), which pathlib parses as
    # path separators and strips everything before them. Drop only the
    # known video extensions at the end.
    stem_path = re.sub(r"\.(mp4|mov|m4v|webm)$", "", filename, flags=re.IGNORECASE)
    stem_path = _DRIVE_DUP_RE.sub("", stem_path)

    # Normalise to a space-delimited, lowercase form BEFORE running regexes.
    # Python's \b treats `_`, `-`, `.` as word characters when adjacent to
    # letters, so "liver_ad15" has no word boundary between "liver" and
    # "_". We flatten all separators to spaces so \b works naturally.
    lower = stem_path.lower()
    normalized_for_match = re.sub(r"[\s_\-./\\]+", " ", lower).strip()
    normalized_for_match = _DATE_RE.sub(" ", normalized_for_match)

    product = _extract_first(normalized_for_match, _PRODUCTS)
    product = _PRODUCT_ALIASES.get(product, product) if product else None

    ad_match = _AD_NUM_RE.search(normalized_for_match)
    ad_num = int(ad_match.group(1)) if ad_match else None

    # Handle "productN" / "product N" mashes ("thyroid9", "liver 6", "wht2",
    # "diabetes4") where the ad number is glued onto the product keyword.
    # This second pass fills in whichever field was missing on the first
    # pass — the two ad_num candidates agree in every observed case.
    if product is None or ad_num is None:
        glued = _find_product_with_number(normalized_for_match)
        if glued is not None:
            glued_product, glued_num = glued
            if product is None:
                product = _PRODUCT_ALIASES.get(glued_product, glued_product)
            if ad_num is None:
                ad_num = glued_num

    geo = _extract_geo(normalized_for_match)
    editor = _extract_first(normalized_for_match, editors)

    variant_tags = tuple(
        _normalise_variant_tag(m.group(0))
        for m in _VARIANT_RE.finditer(normalized_for_match)
    )

    # Build a compact stem for logs/display: normalised space-separated form,
    # with editor removed (editors aren't part of the content identity).
    stem = normalized_for_match
    if editor:
        stem = re.sub(rf"\b{re.escape(editor)}\b", "", stem)
    stem = re.sub(r"\s+", "_", stem).strip("_")

    variant_group = _variant_group_key(product, ad_num, geo)

    return ParsedFilename(
        raw=raw,
        stem=stem,
        product=product,
        ad_num=ad_num,
        geo=geo,
        editor=editor,
        variant_tags=variant_tags,
        variant_group=variant_group,
    )


def _extract_first(lower: str, candidates: tuple[str, ...]) -> str | None:
    """Return the first candidate found as a whole-word match."""
    for c in candidates:
        if re.search(rf"\b{re.escape(c)}\b", lower):
            return c
    return None


def _find_product_with_number(lower: str) -> tuple[str, int] | None:
    """Catch 'productN' / 'product N' mashes like 'thyroid9', 'liver 6'.

    Returns (product, number) for the first match, applying the same
    preference order as _PRODUCTS (longest/most-specific first).
    """
    for c in _PRODUCTS:
        m = re.search(rf"\b{re.escape(c)}\s*0*(\d+)\b", lower)
        if m:
            return c, int(m.group(1))
    return None


def _extract_geo(lower: str) -> str | None:
    for c in _GEOS:
        if re.search(rf"\b{re.escape(c)}\b", lower):
            if c == "usa":
                return "us"
            return c
    return None


def _normalise_variant_tag(match: str) -> str:
    """Tighten whitespace inside a matched variant tag ('var 3' → 'var3')."""
    return re.sub(r"\s+", "", match).lower()


def _variant_group_key(
    product: str | None, ad_num: int | None, geo: str | None
) -> str:
    """Compose the key used for visual-duplicate rotation.

    Falls back to whichever fields exist — a file with no ad_num still
    clusters with other same-product+geo files. Files missing every
    parseable field are placed in a single "unknown" group (the scheduler
    treats them as fully fungible, which is the safest default).
    """
    parts: list[str] = []
    if product:
        parts.append(product)
    if ad_num is not None:
        parts.append(f"ad{ad_num}")
    if geo:
        parts.append(geo)
    return "_".join(parts) if parts else "unknown"
