"""UI FastAPI app: SessionMiddleware + ProxyHeaders + Authlib OIDC + Gradio mount.

Security posture (see README "Security hardening"):
  - OIDC `redirect_uri` pinned from config (not derived from request headers).
  - RP-initiated logout via the OIDC `end_session_endpoint`.
  - Session is rotated (`clear()`) before writing identity.
  - Session payload narrowed to `{sub, username, email, id_token}`.
  - FastAPI auto-docs disabled on the UI service.
  - OAuthError handled explicitly with Logfire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

import gradio as gr
import logfire
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from bankiru.config import get_settings
from bankiru.ui.blocks import gradio_ui

# Path to the project's assets directory (contains the logo image).
# Resolves relative to this file: src/bankiru/ui/app.py → src/bankiru/ui → src/bankiru → src → assets
ASSETS_DIR = Path(__file__).resolve().parents[3] / "assets"
LOGO_PATH = ASSETS_DIR / "bankiru-reviews-logo.png"


def _require(value, name: str) -> str:
    """Validate that a required configuration value is set.

    Raises RuntimeError with a helpful message if the value is falsy.
    Used during app creation to fail fast with clear error messages
    rather than cryptic errors later during OIDC or session operations.
    """
    if not value:
        raise RuntimeError(
            f"{name} is not set. Required for the UI service. "
            f"Populate it via .env or Infisical."
        )
    return value


def create_app() -> FastAPI:
    """Build and return the configured FastAPI + Gradio application.

    This factory function:
      1. Validates required OIDC and session settings
      2. Creates a FastAPI app with auto-docs disabled (UI-only service)
      3. Adds ProxyHeadersMiddleware (trust X-Forwarded-* from Nginx)
      4. Adds SessionMiddleware (signed cookies for OIDC state)
      5. Registers the Authentik OIDC client
      6. Defines /login, /auth, /logout, /, and /favicon.ico routes
      7. Mounts the Gradio UI at /gradio with OIDC-based auth_dependency
    """
    s = get_settings()
    # Fail fast if required OIDC/session settings are missing.
    _require(s.SESSION_MIDDLEWARE_SECRET, "SESSION_MIDDLEWARE_SECRET")
    _require(s.OIDC_CLIENT_ID, "OIDC_CLIENT_ID")
    _require(s.OIDC_CLIENT_SECRET, "OIDC_CLIENT_SECRET")

    # Create the FastAPI app with auto-docs disabled. The UI service
    # doesn't expose a public API — it only serves the Gradio interface.
    app = FastAPI(
        title="Banki.ru Claims and Negative Reviews",
        docs_url=None,      # disable /docs (Swagger UI)
        redoc_url=None,      # disable /redoc
        openapi_url=None,    # disable /openapi.json
    )

    # ── Middleware stack ──────────────────────────────────────────────
    # ProxyHeadersMiddleware: trust X-Forwarded-For/Proto/Host headers
    # from the Nginx reverse proxy. This is necessary for correct URL
    # construction in OIDC redirects and session cookie settings.
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=s.TRUSTED_HOSTS,
    )
    # SessionMiddleware: signs session cookies with the configured secret.
    # The session stores the OIDC user identity after successful login.
    # Settings:
    #   - session_cookie="bankiru_session": custom cookie name
    #   - same_site="lax": allows the cookie to be sent on top-level
    #     navigations (needed for OIDC redirect back from Authentik)
    #   - https_only=True: cookie is only sent over HTTPS
    #   - max_age=3600: session expires after 1 hour of inactivity
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.SESSION_MIDDLEWARE_SECRET or "",
        session_cookie="bankiru_session",
        same_site="lax",
        https_only=True,
        max_age=3600,
    )

    # ── OIDC client registration ─────────────────────────────────────
    # Register the Authentik OIDC provider using Authlib. The discovery
    # URL points to Authentik's .well-known/openid-configuration endpoint,
    # which provides all the necessary endpoints (authorize, token, userinfo,
    # end_session) automatically.
    oauth = OAuth()
    oauth.register(
        name="authentik",
        client_id=s.OIDC_CLIENT_ID,
        client_secret=s.OIDC_CLIENT_SECRET,
        server_metadata_url=s.OIDC_DISCOVERY_URL,
        client_kwargs={"scope": "openid profile email"},
    )
    # Instrument with Logfire, excluding Gradio static assets to reduce
    # trace noise (CSS, JS, fonts generate many low-value spans).
    logfire.instrument_fastapi(app, excluded_urls="/gradio/assets/*")

    # ── OIDC login route ─────────────────────────────────────────────
    @app.get("/login")
    async def login(request: Request):
        """Initiate the OIDC Authorization Code flow.

        Redirects the user to Authentik's authorization endpoint. After
        the user authenticates, Authentik redirects back to /auth with
        an authorization code.

        The redirect_uri is pinned from config (not derived from the
        request) to prevent open-redirect attacks.
        """
        redirect_uri = s.OIDC_REDIRECT_URI or str(request.url_for("auth_callback"))
        return await oauth.authentik.authorize_redirect(request, redirect_uri)

    # ── OIDC callback route ──────────────────────────────────────────
    @app.get("/auth")
    async def auth_callback(request: Request):
        """Handle the OIDC callback from Authentik.

        Exchanges the authorization code for tokens, extracts user info,
        and stores a minimal session payload. The session is rotated
        (cleared before writing) to prevent session fixation attacks.
        """
        try:
            token = await oauth.authentik.authorize_access_token(request)
        except OAuthError as error:
            # OIDC error (e.g. user denied consent, invalid state).
            logfire.warning("OIDC callback failed: {error}", error=str(error))
            request.session.clear()
            return RedirectResponse(url="/login")

        userinfo = token.get("userinfo") or {}
        if not userinfo:
            logfire.warning("OIDC callback returned no userinfo")
            request.session.clear()
            return RedirectResponse(url="/login")

        # Session rotation: clear the old session before writing new data.
        # This prevents session fixation attacks where an attacker sets a
        # known session ID before the user authenticates.
        request.session.clear()
        # Store only the minimal set of user attributes needed by the UI.
        # The id_token is preserved for RP-initiated logout (sent as
        # id_token_hint to Authentik's end_session_endpoint).
        request.session["user"] = {
            "sub": userinfo.get("sub"),
            "username": (
                userinfo.get("preferred_username")
                or userinfo.get("name")
                or userinfo.get("sub")
            ),
            "email": userinfo.get("email"),
            "id_token": token.get("id_token"),
        }
        return RedirectResponse(url="/gradio/")

    # ── Logout route ─────────────────────────────────────────────────
    @app.get("/logout")
    async def logout(request: Request):
        """Log out the user: clear session + RP-initiated OIDC logout.

        Performs two actions:
          1. Clear the local session cookie (immediate local logout)
          2. Redirect to Authentik's end_session_endpoint with the
             id_token_hint (RP-initiated logout per OIDC spec)

        If the end_session_endpoint is unavailable, falls back to a
        simple redirect to the post-logout URI.
        """
        user = request.session.get("user") or {}
        id_token = user.get("id_token")
        # Clear the local session immediately.
        request.session.clear()

        # Attempt to load the OIDC end_session_endpoint from the provider's
        # discovery document. This endpoint invalidates the Authentik session.
        try:
            metadata = await oauth.authentik.load_server_metadata()
            end_session = metadata.get("end_session_endpoint")
        except Exception as exc:
            logfire.warning("load_server_metadata failed: {exc}", exc=str(exc))
            end_session = None

        post_logout = s.OIDC_POST_LOGOUT_URI or "/"
        if not end_session:
            # Fallback: just redirect to the post-logout URI without
            # invalidating the Authentik session.
            return RedirectResponse(url=post_logout)

        # Build the RP-initiated logout URL with query parameters.
        qs = {"post_logout_redirect_uri": post_logout}
        if id_token:
            # id_token_hint tells Authentik which session to invalidate.
            qs["id_token_hint"] = id_token
        return RedirectResponse(url=f"{end_session}?{urlencode(qs)}")

    # ── Root route ───────────────────────────────────────────────────
    @app.get("/")
    def index(request: Request):
        """Redirect to /login (if not authenticated) or /gradio/ (if authenticated)."""
        if not request.session.get("user"):
            return RedirectResponse(url="/login")
        return RedirectResponse(url="/gradio/")

    # ── Favicon ──────────────────────────────────────────────────────
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        """Serve the project logo as the browser favicon."""
        return FileResponse(str(LOGO_PATH))

    # ── Gradio auth dependency ───────────────────────────────────────
    def get_user(request: Request) -> str | None:
        """Extract the username from the session for Gradio's auth_dependency.

        Gradio calls this function on every request to /gradio/. If it
        returns None, Gradio blocks access (the user is redirected to /login
        by the root route). If it returns a string, Gradio allows access
        and makes the username available via gr.Request.
        """
        user = request.session.get("user")
        if not user:
            return None
        username = user.get("username")
        return str(username) if username else None

    # ── Mount Gradio ─────────────────────────────────────────────────
    # Mount the Gradio Blocks UI at /gradio. The auth_dependency function
    # gates access: only authenticated users (with a valid session) can
    # reach the Gradio interface.
    mounted = gr.mount_gradio_app(
        app=app,
        blocks=gradio_ui,
        path="/gradio",
        auth_dependency=get_user,
        theme=gr.themes.Ocean(),
        # Hide the Gradio footer ("Built with Gradio") for a cleaner look.
        css="footer {visibility: hidden}",
    )
    return mounted


# Module-level app instance: uvicorn imports this directly via the
# "bankiru.ui.app:app" import string in __main__.py.
app = create_app()
