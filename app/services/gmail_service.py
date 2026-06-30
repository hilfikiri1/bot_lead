"""
gmail_service.py
Creates Gmail drafts using the Gmail API. Never sends automatically.
"""

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
]


def _get_credentials() -> Credentials:
    """Load or refresh Google OAuth credentials."""
    token_path = Path(settings.google_token_file)
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.google_credentials_file, SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    return creds


def _gmail_service():
    return build("gmail", "v1", credentials=_get_credentials())


def create_draft(
    to: str,
    subject: str,
    body: str,
    sender: str = "me",
) -> str:
    """
    Create a Gmail draft (does NOT send).
    Returns the draft ID.
    """
    try:
        service = _gmail_service()
        mime = MIMEText(body, "plain", "utf-8")
        mime["to"] = to
        mime["subject"] = subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        draft = (
            service.users()
            .drafts()
            .create(
                userId="me",
                body={"message": {"raw": raw}},
            )
            .execute()
        )
        draft_id = draft["id"]
        logger.info("Gmail draft created: %s", draft_id)
        return draft_id
    except HttpError as e:
        logger.error("Gmail API error: %s", e)
        raise
