"""
Google Drive attachment storage for sundry purchase invoices (Registers &
Reminders spec). Uses a dedicated personal Gmail account, authorized once via
OAuth (see scripts/get_google_drive_token.py) — the refresh token lives only
in env vars, never in the repo. Uploaded files stay in that account's Drive;
only the returned file id / view link is stored in Postgres, so the DB never
grows with image/PDF bytes.

Scope is drive.file (not full drive) — the app can only see files it created.
"""
import io
import os

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveNotConfigured(Exception):
    """GOOGLE_DRIVE_* env vars aren't set — run scripts/get_google_drive_token.py once."""


def is_configured() -> bool:
    return bool(
        os.getenv("GOOGLE_DRIVE_CLIENT_ID")
        and os.getenv("GOOGLE_DRIVE_CLIENT_SECRET")
        and os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN")
    )


def _credentials() -> Credentials:
    client_id = os.getenv("GOOGLE_DRIVE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_DRIVE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise DriveNotConfigured(
            "Invoice storage isn't set up yet — GOOGLE_DRIVE_CLIENT_ID/"
            "CLIENT_SECRET/REFRESH_TOKEN missing. Run "
            "scripts/get_google_drive_token.py once to authorize the "
            "dedicated Drive account."
        )
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=_SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return creds


def upload_invoice(data: bytes, filename: str, mime_type: str) -> dict:
    """Upload one invoice file to the dedicated Drive account.
    Returns {"file_id": ..., "view_link": ...}.
    Raises DriveNotConfigured if the account isn't wired up, or the
    underlying googleapiclient error on any upload failure."""
    creds = _credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    metadata = {"name": filename}
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    file = service.files().create(
        body=metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    return {"file_id": file["id"], "view_link": file.get("webViewLink")}
