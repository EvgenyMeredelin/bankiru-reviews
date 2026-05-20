"""UI entrypoint: configure Logfire auto-tracing, then launch uvicorn."""

from __future__ import annotations

from bankiru.config import get_settings
from bankiru.logging import configure_logfire, install_auto_tracing


def main() -> None:
    configure_logfire(service_name="ui")
    install_auto_tracing(["bankiru.ui.app", "bankiru.ui.blocks"])

    import uvicorn

    uvicorn.run(
        app="bankiru.ui.app:app",
        host="0.0.0.0",  # noqa: S104  (container, bound to 127.0.0.1 on host)
        port=get_settings().UI_PORT,
    )


if __name__ == "__main__":
    main()
