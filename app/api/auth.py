"""
auth.py — Google OAuth2 flow for Gmail & Calendar access.
"""
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from google_auth_oauthlib.flow import Flow

from app.config import get_settings
from app.services.gmail_service import SCOPES

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


@router.get("/google")
async def google_login():
    """Redirect the manager to Google OAuth consent screen."""
    try:
        flow = Flow.from_client_secrets_file(
            settings.google_credentials_file,
            scopes=SCOPES,
            redirect_uri=settings.google_redirect_uri,
        )
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        return RedirectResponse(url=auth_url)
    except FileNotFoundError:
        return {"error": "Google credentials file not found. Please configure GOOGLE_CREDENTIALS_FILE."}
    except Exception as e:
        return {"error": f"Google OAuth error: {str(e)}"}


@router.get("/google/callback")
async def google_callback(request: Request):
    """Handle Google OAuth callback and save credentials."""
    from pathlib import Path
    from google.oauth2.credentials import Credentials

    try:
        flow = Flow.from_client_secrets_file(
            settings.google_credentials_file,
            scopes=SCOPES,
            redirect_uri=settings.google_redirect_uri,
        )
        flow.fetch_token(authorization_response=str(request.url))
        creds = flow.credentials
        token_path = Path(settings.google_token_file)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        return HTMLResponse("<h2>✅ Google authorization successful! You can close this tab.</h2>")
    except FileNotFoundError:
        return HTMLResponse("<h2>❌ Google credentials file not found.</h2>")
    except Exception as e:
        return HTMLResponse(f"<h2>❌ Google OAuth error: {str(e)}</h2>")

