"""YouTube Shorts uploader (Phase 1 platform).

Auth: OAuth2 via google-auth-oauthlib InstalledAppFlow.
Token cached to {video_storage_path}/credentials/youtube_token.json.

Returns (video_id, video_url).
"""
from __future__ import annotations

import json
import pathlib

import structlog

from social_signal.config import brand_config, settings
from social_signal.db.models import ContentScript
from social_signal.db.session import _session_factory

log = structlog.get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_API_SERVICE = "youtube"
YOUTUBE_API_VERSION = "v3"


async def upload_short(
    file_path: str,
    script_id: str,
    brand_id: str | None = None,
) -> tuple[str, str | None]:
    """Upload a video as a YouTube Short. Returns (video_id, video_url)."""
    import google.auth.transport.requests
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _load_credentials()
    if not creds:
        raise RuntimeError(
            "YouTube credentials not found. Run: python -m social_signal.platforms.youtube --auth"
        )

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        _save_credentials(creds)

    youtube = build(YOUTUBE_API_SERVICE, YOUTUBE_API_VERSION, credentials=creds)

    title, description, tags = await _build_metadata(script_id, brand_id=brand_id)
    yt_cfg = brand_config(brand_id).get("platforms", {}).get("youtube", {})
    privacy = yt_cfg.get("privacy_status", "public")
    category_id = yt_cfg.get("category_id", "28")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id", "")
    video_url = f"https://youtube.com/shorts/{video_id}" if video_id else None
    log.info("youtube.uploaded", video_id=video_id, url=video_url)
    return video_id, video_url


async def _build_metadata(
    script_id: str,
    brand_id: str | None = None,
) -> tuple[str, str, list[str]]:
    factory = _session_factory()
    async with factory() as session:
        cs = await session.get(ContentScript, script_id) if script_id else None

    yt_cfg = brand_config(brand_id).get("platforms", {}).get("youtube", {})
    default_tags: list[str] = yt_cfg.get("default_tags", ["shorts", "example"])

    if cs:
        # Derive title from script (first sentence, truncated, + #shorts)
        first_sentence = cs.script_body.split(".")[0][:80].strip()
        title = f"{first_sentence} #shorts"
        description = cs.script_body[:300] + "\n\n#GlitchExecutor #AlgoTrading"
    else:
        title = "this project #shorts"
        description = "Algorithmic trading AI — example.com\n\n#GlitchExecutor #AlgoTrading"

    return title, description, default_tags


def _credentials_path() -> pathlib.Path:
    storage = pathlib.Path(settings().video_storage_path)
    creds_dir = storage / "credentials"
    creds_dir.mkdir(parents=True, exist_ok=True)
    return creds_dir / "youtube_token.json"


def _load_credentials():
    from google.oauth2.credentials import Credentials
    path = _credentials_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Credentials.from_authorized_user_info(data, SCOPES)


def _save_credentials(creds) -> None:
    path = _credentials_path()
    path.write_text(creds.to_json())


if __name__ == "__main__":
    import sys
    if "--auth" in sys.argv:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(
            settings().youtube_client_secrets_file, SCOPES
        )
        creds = flow.run_local_server(port=0)
        _save_credentials(creds)
        print(f"Credentials saved to {_credentials_path()}")
