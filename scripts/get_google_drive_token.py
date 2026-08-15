"""
One-time script to authorize OrdeRR's dedicated Google account for storing
sundry-purchase invoice attachments in Drive.

Setup (once, in Google Cloud Console):
  1. Create/select a project, enable the "Google Drive API".
  2. Create OAuth client credentials of type "Desktop app" — gives you a
     client ID + client secret.
  3. Configure the OAuth consent screen in "Testing" mode and add the
     dedicated Gmail address as a test user (avoids Google's app-review
     process, fine for single-account internal use).

Run this locally (needs a browser):

    GOOGLE_DRIVE_CLIENT_ID=... GOOGLE_DRIVE_CLIENT_SECRET=... \
        python scripts/get_google_drive_token.py

A browser window opens — sign in as the DEDICATED account (not your personal
one) and approve access. The script then prints a refresh token: add it to
.env / Render's environment as GOOGLE_DRIVE_REFRESH_TOKEN, alongside the same
GOOGLE_DRIVE_CLIENT_ID / GOOGLE_DRIVE_CLIENT_SECRET used above.

Optional: set GOOGLE_DRIVE_FOLDER_ID to the id of a specific Drive folder
(from its URL) to keep invoice uploads in one place; omit to upload to the
account's Drive root.
"""
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    client_id = os.environ["GOOGLE_DRIVE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_DRIVE_CLIENT_SECRET"]
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0)
    print("\nAdd this to your .env / Render environment:\n")
    print(f"GOOGLE_DRIVE_REFRESH_TOKEN={creds.refresh_token}")


if __name__ == "__main__":
    main()
