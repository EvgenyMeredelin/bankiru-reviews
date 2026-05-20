"""API entrypoint: configure Logfire auto-tracing, then launch uvicorn."""

from __future__ import annotations

from bankiru.config import get_settings
from bankiru.logging import configure_logfire, install_auto_tracing


def main() -> None:
    configure_logfire(service_name="api")
    install_auto_tracing(["bankiru.api.routes", "bankiru.api.handlers"])

    import uvicorn

    uvicorn.run(
        app="bankiru.api.app:app",
        host="0.0.0.0",  # noqa: S104  (container, bound by compose ports:)
        port=get_settings().API_PORT,
    )


if __name__ == "__main__":
    main()
