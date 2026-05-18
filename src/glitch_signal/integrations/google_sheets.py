"""Google Sheets output sink for per-brand per-task progress tracking.

Each (brand, task) can declare a Google Sheet in its `outputs` block:

    "outputs": {
      "google_sheet": {
        "sheet_id":  "<spreadsheet id>",
        "worksheet": "Sheet1"
      }
    }

The service account used for Drive (credentials/drive-sa.json) is reused
here — just with the extra `spreadsheets` scope. The target sheet must
be shared with the SA's email as an editor (or owner).

This module owns a small, task-agnostic API:

- ensure_header(sheet_id, worksheet, columns)     # idempotent header write
- append_row(sheet_id, worksheet, columns, row)   # add a row (by column name dict)
- update_row_by_key(sheet_id, worksheet, key_col, key, updates)   # partial update
- read_row_by_key(sheet_id, worksheet, columns, key_col, key)     # fetch one row as dict

The drive_scout / publisher / caption_writer nodes import these to keep
the sheet in sync with DB state. No polling — pushes happen at the
moments state transitions in the agent.

Calls are synchronous inside an asyncio.to_thread wrapper so the
scheduler event loop doesn't block on Google's HTTP roundtrip.
"""
from __future__ import annotations

import asyncio
import pathlib
import time
from functools import lru_cache, wraps
from typing import Any

import structlog

from glitch_signal.config import settings

log = structlog.get_logger(__name__)

_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Google Sheets per-user-per-project write quota is 60/min. The first
# drive_scout tick on a 65-file brand or a mass reconciliation will
# blow straight through that. Wrap every write with exponential backoff
# on 429 (also covers transient 5xx) so the agent self-heals.
_RETRY_ON_HTTP_CODES = {429, 500, 502, 503}
_MAX_RETRIES = 5
_BASE_BACKOFF_S = 2.0


def _with_retry(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from googleapiclient.errors import HttpError

        for attempt in range(_MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except HttpError as exc:
                status = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "resp", None), "status", None
                )
                if status not in _RETRY_ON_HTTP_CODES or attempt == _MAX_RETRIES - 1:
                    raise
                delay = _BASE_BACKOFF_S * (2 ** attempt)
                log.info(
                    "google_sheets.retry_backoff",
                    status=status, attempt=attempt + 1, delay_s=delay,
                )
                time.sleep(delay)
        # Unreachable — loop either returns or raises.
        raise RuntimeError("google_sheets: retry loop exited without outcome")
    return wrapped


@lru_cache(maxsize=1)
def _service():
    """Build and memoise a Sheets API client using the shared SA creds."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    s = settings()
    sa_path = s.google_drive_sa_json
    if not sa_path:
        raise RuntimeError(
            "google_sheets: GOOGLE_DRIVE_SA_JSON is not set — the sheets "
            "sink reuses the same service account as drive_scout."
        )
    if not pathlib.Path(sa_path).exists():
        raise RuntimeError(
            f"google_sheets: service-account JSON not found at {sa_path!r}"
        )

    creds = service_account.Credentials.from_service_account_file(
        sa_path, scopes=[_SHEETS_SCOPE],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def ensure_header(
    sheet_id: str, worksheet: str, columns: list[str]
) -> None:
    """Write `columns` to row 1 if the sheet is empty or the header differs.

    Idempotent — safe to call on every drive_scout tick. Existing data
    rows are never touched; we only rewrite row 1 when the schema
    doesn't match.
    """
    await asyncio.to_thread(_ensure_header_sync, sheet_id, worksheet, columns)


async def append_row(
    sheet_id: str,
    worksheet: str,
    columns: list[str],
    row: dict[str, Any],
) -> None:
    """Append a row using the column order passed in."""
    await asyncio.to_thread(_append_row_sync, sheet_id, worksheet, columns, row)


async def update_row_by_key(
    sheet_id: str,
    worksheet: str,
    columns: list[str],
    key_column: str,
    key_value: str,
    updates: dict[str, Any],
) -> bool:
    """Find the row where `key_column == key_value` and apply partial updates.

    Returns True if a row was found and updated, False if no matching row.
    """
    return await asyncio.to_thread(
        _update_row_sync, sheet_id, worksheet, columns, key_column, key_value, updates,
    )


async def read_row_by_key(
    sheet_id: str,
    worksheet: str,
    columns: list[str],
    key_column: str,
    key_value: str,
) -> dict[str, str] | None:
    """Return the row where `key_column == key_value` as {column: value}.

    Returns None when no matching row exists. Missing trailing cells come
    back as empty strings so callers can treat the result as a complete
    dict keyed on `columns`.
    """
    return await asyncio.to_thread(
        _read_row_sync, sheet_id, worksheet, columns, key_column, key_value,
    )


# ---------------------------------------------------------------------------
# Sync workers — called via asyncio.to_thread
# ---------------------------------------------------------------------------

@_with_retry
def _ensure_header_sync(sheet_id: str, worksheet: str, columns: list[str]) -> None:
    svc = _service()
    rng = f"'{worksheet}'!1:1"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng,
    ).execute()
    existing = (resp.get("values") or [[]])[0]
    if existing == columns:
        return
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=rng,
        valueInputOption="RAW",
        body={"values": [columns]},
    ).execute()
    log.info(
        "google_sheets.header_written",
        sheet_id=sheet_id,
        worksheet=worksheet,
        columns=columns,
    )


@_with_retry
def _append_row_sync(
    sheet_id: str, worksheet: str, columns: list[str], row: dict[str, Any]
) -> None:
    svc = _service()
    values = [[_stringify(row.get(col, "")) for col in columns]]
    # Use a full-row range for the append anchor so Sheets' "last row"
    # detection considers every column, not just A. Single-column A:A
    # anchors misbehave on freshly-created sheets with 1000 pre-provisioned
    # empty rows — append lands at row ~60 instead of row 2 and the
    # operator thinks the sheet is empty.
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"'{worksheet}'!A:K",
        valueInputOption="USER_ENTERED",   # allow hyperlink / date parsing
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    log.info(
        "google_sheets.row_appended",
        sheet_id=sheet_id,
        worksheet=worksheet,
        key_preview=next((v for v in values[0] if v), ""),
    )


@_with_retry
def _update_row_sync(
    sheet_id: str,
    worksheet: str,
    columns: list[str],
    key_column: str,
    key_value: str,
    updates: dict[str, Any],
) -> bool:
    svc = _service()
    if key_column not in columns:
        raise ValueError(
            f"google_sheets: key_column {key_column!r} not in {columns}"
        )
    key_col_idx = columns.index(key_column)

    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{worksheet}'!A2:{_col_letter(len(columns))}",
    ).execute()
    rows = resp.get("values", []) or []

    target_row = None
    for i, r in enumerate(rows):
        if len(r) > key_col_idx and r[key_col_idx] == key_value:
            target_row = i + 2    # +2: +1 for 1-based, +1 for header
            break
    if target_row is None:
        log.warning(
            "google_sheets.no_matching_row",
            sheet_id=sheet_id,
            worksheet=worksheet,
            key_column=key_column,
            key_value=key_value,
        )
        return False

    # Read the current row, apply updates, rewrite. Doing it this way
    # keeps columns not in `updates` untouched and respects the sheet's
    # existing formatting.
    row_range = f"'{worksheet}'!A{target_row}:{_col_letter(len(columns))}{target_row}"
    current = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=row_range,
    ).execute().get("values", [[]])[0]
    # Pad to column count.
    current = list(current) + [""] * (len(columns) - len(current))
    for col, val in updates.items():
        if col not in columns:
            continue
        current[columns.index(col)] = _stringify(val)

    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=row_range,
        valueInputOption="USER_ENTERED",
        body={"values": [current]},
    ).execute()
    log.info(
        "google_sheets.row_updated",
        sheet_id=sheet_id,
        worksheet=worksheet,
        row=target_row,
        updated_keys=list(updates.keys()),
    )
    return True


@_with_retry
def _read_row_sync(
    sheet_id: str,
    worksheet: str,
    columns: list[str],
    key_column: str,
    key_value: str,
) -> dict[str, str] | None:
    svc = _service()
    if key_column not in columns:
        raise ValueError(
            f"google_sheets: key_column {key_column!r} not in {columns}"
        )
    key_col_idx = columns.index(key_column)

    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{worksheet}'!A2:{_col_letter(len(columns))}",
    ).execute()
    rows = resp.get("values", []) or []

    for r in rows:
        if len(r) > key_col_idx and r[key_col_idx] == key_value:
            padded = list(r) + [""] * (len(columns) - len(r))
            return {col: padded[i] for i, col in enumerate(columns)}
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return str(v)


def _col_letter(n: int) -> str:
    """1-based column index → spreadsheet letter (1→A, 27→AA)."""
    letters = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters
