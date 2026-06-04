"""API entrypoint: configure Logfire auto-tracing, then launch uvicorn.

This module is executed when the API service starts via:
    python -m bankiru.api

It is the first code that runs in the API container. The startup sequence is:
  1. Configure Logfire observability (service_name="api") so all subsequent
     log/trace calls are tagged with the correct service identity.
  2. Install auto-tracing on the routes and handlers modules — this wraps
     every function in those modules with OpenTelemetry spans automatically,
     giving full call-tree visibility in the Logfire dashboard without
     requiring manual span annotations in every function.
  3. Import and launch uvicorn, which loads the FastAPI app from
     bankiru.api.app:app. The app factory (create_app) handles the rest:
     lifespan events (DB bootstrap, embedding backfill), route registration,
     and Logfire FastAPI instrumentation.

Design note: uvicorn is imported *inside* main() (not at module level) to
ensure Logfire auto-tracing is installed before any application code is
imported. If uvicorn were imported at the top, it would trigger the import
of bankiru.api.app (and transitively routes/handlers) before auto-tracing
is active, and those modules would not be instrumented.
"""

from __future__ import annotations

from bankiru.config import get_settings
from bankiru.logging import configure_logfire, install_auto_tracing


def main() -> None:
    """Bootstrap observability, then start the uvicorn ASGI server."""
    # Step 1: Wire up Logfire with service identity "api".
    configure_logfire(service_name="api")
    # Step 2: Auto-instrument routes and handlers — every function call in
    # these modules will emit an OpenTelemetry span automatically.
    install_auto_tracing(["bankiru.api.routes", "bankiru.api.handlers"])

    # Step 3: Import uvicorn late (after auto-tracing is installed) so that
    # the app module and its transitive imports are instrumented.
    import uvicorn

    # Launch the ASGI server. host="0.0.0.0" binds to all interfaces inside
    # the container; the docker-compose.yml `ports:` directive controls which
    # host interfaces the port is published on.
    uvicorn.run(
        app="bankiru.api.app:app",
        host="0.0.0.0",  # noqa: S104  (container, bound by compose ports:)
        port=get_settings().API_PORT,
    )


if __name__ == "__main__":
    main()
