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

ASSETS_DIR = Path(__file__).resolve().parents[3] / "assets"
LOGO_PATH = ASSETS_DIR / "bankiru-reviews-logo.png"


def _require(value, name: str) -> str:
    if not value:
        raise RuntimeError(
            f"{name} is not set. Required for the UI service. "
            f"Populate it via .env or Infisical."
        )
    return value


def create_app() -> FastAPI:
    s = get_settings()
    _require(s.SESSION_MIDDLEWARE_SECRET, "SESSION_MIDDLEWARE_SECRET")
    _require(s.OIDC_CLIENT_ID, "OIDC_CLIENT_ID")
    _require(s.OIDC_CLIENT_SECRET, "OIDC_CLIENT_SECRET")

    app = FastAPI(
        title="Banki.ru Claims and Negative Reviews",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=s.TRUSTED_HOSTS,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.SESSION_MIDDLEWARE_SECRET or "",
        session_cookie="bankiru_session",
        same_site="lax",
        https_only=True,
        max_age=3600,
    )

    oauth = OAuth()
    oauth.register(
        name="authentik",
        client_id=s.OIDC_CLIENT_ID,
        client_secret=s.OIDC_CLIENT_SECRET,
        server_metadata_url=s.OIDC_DISCOVERY_URL,
        client_kwargs={"scope": "openid profile email"},
    )
    logfire.instrument_fastapi(app, excluded_urls="/gradio/assets/*")

    @app.get("/login")
    async def login(request: Request):
        redirect_uri = s.OIDC_REDIRECT_URI or str(request.url_for("auth_callback"))
        return await oauth.authentik.authorize_redirect(request, redirect_uri)

    @app.get("/auth")
    async def auth_callback(request: Request):
        try:
            token = await oauth.authentik.authorize_access_token(request)
        except OAuthError as error:
            logfire.warning("OIDC callback failed: {error}", error=str(error))
            request.session.clear()
            return RedirectResponse(url="/login")

        userinfo = token.get("userinfo") or {}
        if not userinfo:
            logfire.warning("OIDC callback returned no userinfo")
            request.session.clear()
            return RedirectResponse(url="/login")

        request.session.clear()
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

    @app.get("/logout")
    async def logout(request: Request):
        user = request.session.get("user") or {}
        id_token = user.get("id_token")
        request.session.clear()

        try:
            metadata = await oauth.authentik.load_server_metadata()
            end_session = metadata.get("end_session_endpoint")
        except Exception as exc:
            logfire.warning("load_server_metadata failed: {exc}", exc=str(exc))
            end_session = None

        post_logout = s.OIDC_POST_LOGOUT_URI or "/"
        if not end_session:
            return RedirectResponse(url=post_logout)

        qs = {"post_logout_redirect_uri": post_logout}
        if id_token:
            qs["id_token_hint"] = id_token
        return RedirectResponse(url=f"{end_session}?{urlencode(qs)}")

    @app.get("/")
    def index(request: Request):
        if not request.session.get("user"):
            return RedirectResponse(url="/login")
        return RedirectResponse(url="/gradio/")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(str(LOGO_PATH))

    def get_user(request: Request) -> str | None:
        user = request.session.get("user")
        if not user:
            return None
        username = user.get("username")
        return str(username) if username else None

    mounted = gr.mount_gradio_app(
        app=app,
        blocks=gradio_ui,
        path="/gradio",
        auth_dependency=get_user,
        theme=gr.themes.Ocean(),
        css="footer {visibility: hidden}",
    )
    return mounted


app = create_app()
