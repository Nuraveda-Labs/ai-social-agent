#!/usr/bin/env bash
# Cron entry: fire one due sheet-driven post if pacing allows.
#
# Cron line (recommended every 5 min):
#   */5 * * * * /home/support/ai-social-media-agent/scripts/sheet_posting_tick.sh \
#     >> /home/support/.local/state/ai-social-media-agent/logs/sheet-posting.log 2>&1
#
# Pacing rules inside reader.fetch_next_due (min 4h interval per
# brand+platform, daily cap 2) mean most ticks no-op cleanly. A real
# post only fires when the schedule + pacing converge.

set -euo pipefail
cd /home/support/ai-social-media-agent
set -a; source .env; set +a
source .venv/bin/activate
exec python3 -m scripts.run_sheet_posting_tick
