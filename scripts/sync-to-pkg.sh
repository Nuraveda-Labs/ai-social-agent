#!/usr/bin/env bash
# scripts/sync-to-pkg.sh — TEMPLATE (v2, allowlist-based)
#
# Mirrors ENGINE-ONLY code from the -private repo to the -pkg repo
# (buyer distribution). Uses a strict positive allowlist instead of
# excludes — anything not explicitly listed under ALLOWLIST below
# is left out. This is the failure-safe direction: a forgotten
# entry means buyers miss a feature, not that production state
# leaks into the buyer-pkg.
#
# Lives at scripts/sync-to-pkg.sh inside each -private repo.
# Per-repo customisation: edit PKG_REMOTE + PKG_LOCAL_DIR + the
# language-specific ENGINE_DIR (src/<pkg>) below.

set -euo pipefail

# ── Per-repo config — edit these per SKU ───────────────────────
PKG_REMOTE="git@codeberg.org:glitch-executor/ai_marketing_stack-ai-social-media-agent-pkg.git"
PKG_LOCAL_DIR="${HOME}/ai_marketing_stack-ai-social-media-agent-pkg"
ENGINE_DIR="src"   # change to "app" or "agent" if the engine root differs

# ── Allowlist — what gets shipped to buyers ────────────────────
# Paths relative to the -private working tree. Add/remove with
# care. Anything outside this list is NOT shipped.
ALLOWLIST=(
  "$ENGINE_DIR"          # engine source
  "tests/fixtures"       # synthetic test fixtures only
  "pyproject.toml"
  "package.json"
  "pnpm-workspace.yaml"
  "tsconfig.json"
  "vite.config.js"
  "vite.config.ts"
  "remotion.config.ts"
  ".env.example"
  ".gitignore"
  ".npmrc"
  ".dockerignore"
  ".editorconfig"
)

# ── Files we author per-pkg (preserved across syncs) ────────────
# These live ONLY in -pkg, never in -private. The sync must NOT
# overwrite them.
PRESERVE_IN_PKG=(
  "AGENTS.md"
  "README.md"
  "LICENSE"
  "CHANGELOG.md"
  "brand-configs/demo.yaml"
  ".pkg-init"
)

# ── Boilerplate below — same across SKUs ───────────────────────

PRIVATE_DIR="$(pwd)"
PRIVATE_SHA="$(git -C "$PRIVATE_DIR" rev-parse --short HEAD)"
PRIVATE_NAME="$(basename "$PRIVATE_DIR")"

# Clone or refresh the pkg repo.
if [ ! -d "$PKG_LOCAL_DIR/.git" ]; then
  echo "→ cloning $PKG_REMOTE → $PKG_LOCAL_DIR"
  git clone "$PKG_REMOTE" "$PKG_LOCAL_DIR" || {
    # Empty repo — clone produces an empty dir. Init manually.
    mkdir -p "$PKG_LOCAL_DIR"
    cd "$PKG_LOCAL_DIR"
    git init -b main
    git remote add origin "$PKG_REMOTE"
    cd "$PRIVATE_DIR"
  }
else
  echo "→ refreshing existing $PKG_LOCAL_DIR"
  cd "$PKG_LOCAL_DIR"
  git fetch origin || true
  # If origin has main, hard-reset to it; otherwise keep local main.
  if git ls-remote --exit-code origin main >/dev/null 2>&1; then
    git checkout -B main origin/main
  else
    git checkout -B main
  fi
  cd "$PRIVATE_DIR"
fi

# Stash anything we want to preserve in pkg before wiping.
STASH_DIR="$(mktemp -d)"
for path in "${PRESERVE_IN_PKG[@]}"; do
  if [ -e "$PKG_LOCAL_DIR/$path" ]; then
    mkdir -p "$STASH_DIR/$(dirname "$path")"
    cp -a "$PKG_LOCAL_DIR/$path" "$STASH_DIR/$path"
  fi
done

# WIPE pkg working tree (everything except .git). Allowlist content
# is re-laid down fresh from -private below; preserved content is
# restored after.
find "$PKG_LOCAL_DIR" -mindepth 1 -maxdepth 1 -not -name '.git' -exec rm -rf {} +

# Copy each allowlisted path from -private → -pkg.
for path in "${ALLOWLIST[@]}"; do
  src="$PRIVATE_DIR/$path"
  if [ -e "$src" ]; then
    dst="$PKG_LOCAL_DIR/$path"
    mkdir -p "$(dirname "$dst")"
    rsync -a \
      --exclude='__pycache__/' \
      --exclude='*.pyc' \
      --exclude='.pytest_cache/' \
      --exclude='.ruff_cache/' \
      --exclude='.venv/' \
      --exclude='venv/' \
      --exclude='node_modules/' \
      --exclude='.next/' \
      --exclude='.react-router/' \
      --exclude='dist/' \
      --exclude='build/' \
      --exclude='.reports/' \
      --exclude='.cache/' \
      --exclude='credentials/' \
      --exclude='playbooks/' \
      --exclude='brand-configs/' \
      --exclude='profiles/' \
      --exclude='tenants/' \
      --exclude='ops-private/' \
      --exclude='var/' \
      --exclude='.env' \
      --exclude='.env.*' \
      --exclude='*.log' \
      --exclude='logs/' \
      --exclude='.DS_Store' \
      "$src" "$(dirname "$dst")/"
  fi
done

# Restore the preserved per-pkg authored files.
for path in "${PRESERVE_IN_PKG[@]}"; do
  if [ -e "$STASH_DIR/$path" ]; then
    mkdir -p "$(dirname "$PKG_LOCAL_DIR/$path")"
    cp -a "$STASH_DIR/$path" "$PKG_LOCAL_DIR/$path"
  fi
done
rm -rf "$STASH_DIR"

# Synthetic demo brand-config (if -private ships one under tests/fixtures).
mkdir -p "$PKG_LOCAL_DIR/brand-configs"
if [ -f "$PRIVATE_DIR/tests/fixtures/demo-brand.yaml" ] && [ ! -f "$PKG_LOCAL_DIR/brand-configs/demo.yaml" ]; then
  cp "$PRIVATE_DIR/tests/fixtures/demo-brand.yaml" "$PKG_LOCAL_DIR/brand-configs/demo.yaml"
fi

# ── Sanitise: scrub real brand/account tokens from synced files ────
# These tokens appear in -private source as production defaults
# but must be neutralised in the buyer-facing -pkg.
declare -A SANITISE_MAP=(
  ['<your-gcp-project>']='your-gcp-project-id'
  ['<your-sa>@<your-project>.iam.gserviceaccount.com']='your-sa@your-gcp-project-id.iam.gserviceaccount.com'
  ['<your-sa>@your-gcp-project-id.iam.gserviceaccount.com']='your-sa@your-gcp-project-id.iam.gserviceaccount.com'
  ['urban-classics-store.com']='your-store.com'
  ['classicoo.in']='your-store-2.com'
  ['storico.in']='your-store-3.com'
  ['trendsetters-store.com']='your-store-4.com'
  ['f51039.myshopify.com']='your-store.myshopify.com'
  ['acmsuy-g0.myshopify.com']='your-second-store.myshopify.com'
  ['ys4n0u-ys.myshopify.com']='your-third-store.myshopify.com'
  ['Urban Classics Store']='Example Store'
  ['Urban Classics']='Example Store'
  ['urban-classics']='example-store'
  ['Storico']='Example Store 2'
  ['storico']='example-store-2'
  ['Classicoo']='Example Store 3'
  ['classicoo']='example-store-3'
  ['Trendsetters']='Example Store 4'
  ['trendsetters']='example-store-4'
  ['example']='Example Pet Brand'
  ['example']='example-pet-brand'
  ['Mokshya']='Example Wellness Brand'
  ['mokshya']='example-wellness-brand'
  ['Namhya']='Example Ayurveda Brand'
  ['namhya']='example-ayurveda-brand'
  ['Nmahya']='Example Ayurveda Brand'
  ['nmahya']='example-ayurveda-brand'
  ['7577617886014537729']='0000000000000000000'
)
echo "→ sanitising real-brand tokens"
while IFS= read -r -d '' f; do
  for token in "${!SANITISE_MAP[@]}"; do
    if grep -qF "$token" "$f" 2>/dev/null; then
      replacement="${SANITISE_MAP[$token]}"
      # Use perl for safe substring substitution (no regex meta worries)
      perl -i -pe "s/\Q$token\E/$replacement/g" "$f"
    fi
  done
done < <(find "$PKG_LOCAL_DIR" -type f -not -path '*/.git/*' \
  \( -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.tsx' -o -name '*.jsx' -o -name '*.mjs' \
     -o -name '*.json' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' \
     -o -name '*.md' -o -name '*.sh' -o -name '.env*' -o -name '*.txt' \) -print0)

# Genericise GTM container IDs and GA4 measurement IDs (any value, not
# just the known set). Buyers must fill in their own IDs anyway.
echo "→ genericising GTM/GA4 IDs"
find "$PKG_LOCAL_DIR" -type f -not -path '*/.git/*' \
  \( -name '*.py' -o -name '*.js' -o -name '*.ts' -o -name '*.tsx' -o -name '*.jsx' -o -name '*.mjs' \
     -o -name '*.json' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' \
     -o -name '*.md' -o -name '*.sh' -o -name '.env*' -o -name '*.txt' \) \
  -exec perl -i -pe 's/GTM-[A-Z0-9]{6,12}/GTM-XXXXXXXX/g; s/\bG-[A-Z0-9]{10}\b/G-XXXXXXXXXX/g' {} \;

# Rename brand-specific files (e.g. auth.classicoo.$.jsx → auth.demo.$.jsx).
# Cover lowercase brand tokens that might appear in filenames.
echo "→ renaming brand-specific files"
for token in urban-classics urban_classics classicoo storico trendsetters example mokshya namhya nmahya; do
  while IFS= read -r f; do
    new="$(echo "$f" | sed "s/$token/demo/g")"
    [ "$f" != "$new" ] && mv "$f" "$new" 2>/dev/null && echo "  $f → $new"
  done < <(find "$PKG_LOCAL_DIR" -type f -not -path '*/.git/*' -iname "*$token*")
done

# ── Secret scan (paranoid; aborts push on hit) ─────────────────
DANGER_PATTERNS=(
  'rzp_live_'
  'sk_live_'
  'EAA[A-Za-z0-9]{40,}'
  'AKIA[A-Z0-9]{16}'
  'xoxb-[0-9]+-[0-9]+'
  'AIza[0-9A-Za-z_-]{35}'
  'ya29\.[A-Za-z0-9_-]{20,}'
  '-----BEGIN (RSA|EC|OPENSSH|DSA|PRIVATE) KEY-----'
  '"private_key":[[:space:]]*"-----BEGIN'
  '"private_key_id":[[:space:]]*"[a-f0-9]{40}'
  '"type":[[:space:]]*"service_account"'
)
HITS=0
for pat in "${DANGER_PATTERNS[@]}"; do
  matches=$(grep -rnE "$pat" "$PKG_LOCAL_DIR" \
    --include='*.yaml' --include='*.yml' --include='*.json' \
    --include='*.env*' --include='*.ts' --include='*.js' --include='*.py' \
    --include='*.sh' --include='*.md' --include='*.toml' --include='*.txt' \
    --exclude-dir='.git' --exclude='sync-to-pkg.sh' 2>/dev/null || true)
  if [ -n "$matches" ]; then
    echo "⚠ POSSIBLE SECRET LEAK matching: $pat"
    echo "$matches" | head -5
    HITS=$((HITS+1))
  fi
done
if [ "$HITS" -gt 0 ]; then
  echo
  echo "ABORTING: $HITS secret-pattern hit(s) in pkg. Investigate before pushing."
  exit 1
fi

# Final tree summary before commit (for human review on canary runs).
echo "── -pkg tree summary ──"
find "$PKG_LOCAL_DIR" -mindepth 1 -maxdepth 2 -not -path '*/\.git*' | sort
echo "── total files: $(find "$PKG_LOCAL_DIR" -type f -not -path '*/.git/*' | wc -l) ──"

# Commit + push.
cd "$PKG_LOCAL_DIR"
git add -A
if git diff --cached --quiet; then
  echo "→ no changes to commit; pkg already at $PRIVATE_NAME @ $PRIVATE_SHA"
  exit 0
fi
git -c user.email=support@example.com -c user.name="an open-source project" \
  commit -m "sync: engine update from $PRIVATE_NAME @ $PRIVATE_SHA"
git push -u origin main

echo "✓ pushed $PRIVATE_NAME @ $PRIVATE_SHA → $PKG_REMOTE"
