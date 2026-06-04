"""UI entrypoint: configure Logfire auto-tracing, then launch uvicorn.

This module is executed when the UI service starts via:
    python -m bankiru.ui

It follows the same startup pattern as the API entrypoint (__main__.py in
bankiru.api):
  1. Configure Logfire observability (service_name="ui")
  2. Install auto-tracing on the app and blocks modules
  3. Import and launch uvicorn (late import to ensure tracing is active)

The UI service is bound to 127.0.0.1 on the host (not 0.0.0.0) via the
docker-compose.yml port mapping. Public access goes through the host's
Nginx reverse proxy, which terminates TLS and adds security headers.

Connection to other modules:
  - bankiru.ui.app    — provides the FastAPI app with OIDC auth + Gradio mount
  - bankiru.config    — provides UI_PORT setting
  - bankiru.logging   — provides Logfire configuration
"""

from __future__ import annotations

from bankiru.config import get_settings
from bankiru.logging import configure_logfire, install_auto_tracing


def main() -> None:
    """Bootstrap observability, then start the uvicorn ASGI server.

    uvicorn is imported inside main() (not at module level) to ensure
    Logfire auto-tracing is installed before any application code is
    imported. This guarantees that bankiru.ui.app and bankiru.ui.blocks
    are fully instrumented with OpenTelemetry spans.
    """
    # Step 1: Wire up Logfire with service identity "ui".
    configure_logfire(service_name="ui")
    # Step 2: Auto-instrument the app and blocks modules.
    install_auto_tracing(["bankiru.ui.app", "bankiru.ui.blocks"])

    # Step 3: Import uvicorn late (after auto-tracing is installed).
    import uvicorn

    # Launch the ASGI server. host="0.0.0.0" binds to all interfaces inside
    # the container; the docker-compose.yml `ports:` directive restricts
    # this to 127.0.0.1 on the host (loopback only).
    uvicorn.run(
        app="bankiru.ui.app:app",
        host="0.0.0.0",  # noqa: S104  (container, bound to 127.0.0.1 on host)
        port=get_settings().UI_PORT,
    )


if __name__ == "__main__":
    main()
