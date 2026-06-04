"""Logfire setup helpers shared between services.

Logfire (https://logfire.pydantic.dev/) is the observability backend used by
this project. It provides structured logging, distributed tracing, and metrics
via an OpenTelemetry-compatible collector.

Every entrypoint calls ``configure_logfire()`` with a service name:
  - ``bankiru.api.__main__``     — long-running API (also auto-traces routes/handlers)
  - ``bankiru.ui.__main__``      — long-running UI (also auto-traces app/blocks)
  - ``bankiru.parser.__main__``  — long-running parser (explicit spans only)
  - ``bankiru.embedder.__main__`` — ad-hoc CLI (explicit spans only; no auto-tracing)

The LOGFIRE_TOKEN env var (optional) controls where traces are sent.
When the token is None, Logfire falls back to local/anonymous mode,
which is useful for development without a remote collector.

Auto-tracing (``install_auto_tracing``) is used by the API and UI services only.
It instruments every function call in the listed module paths with zero-code-change
spans. The embedder CLI cannot auto-trace ``bankiru.embedder`` because
``python -m bankiru.embedder`` loads the package ``__init__`` before ``__main__``.
"""

from __future__ import annotations

import logging

import logfire


def configure_logfire(service_name: str) -> None:
    """Configure Logfire for the current process.

    Sets the service name that appears in the Logfire dashboard (e.g.
    "api", "parser", "ui", "embedder") so traces from different services
    are visually distinguishable.

    Also wires Python's standard logging module into Logfire via
    LogfireLoggingHandler, so any library or application code that uses
    ``logging.getLogger(...)`` automatically emits structured log events
    to the same Logfire backend. This is important because some
    dependencies (e.g. APScheduler, httpx) log via the stdlib logger.
    """
    # Tag every trace/log line with the service name (api, parser, ui, embedder).
    logfire.configure(service_name=service_name)
    # Bridge stdlib logging → Logfire so third-party libraries appear in the
    # same dashboard without switching to the logfire API directly.
    logging.basicConfig(handlers=[logfire.LogfireLoggingHandler()])


def install_auto_tracing(modules: list[str]) -> None:
    """Auto-trace the given modules with no minimum duration.

    Instruments every function/method in the listed module paths (e.g.
    ``["bankiru.api", "bankiru.parser"]``) so that each call automatically
    creates an OpenTelemetry span. ``min_duration=0`` means even fast
    functions are traced — useful for debugging but may increase trace
    volume in production. Adjust if trace costs become a concern.

    Called at API/UI startup alongside configure_logfire(). Each service
    traces only its own module subtree to avoid redundant instrumentation.
    """
    # min_duration=0 traces every call (even sub-millisecond helpers). Good for
    # debugging; increase if trace volume becomes costly in production.
    logfire.install_auto_tracing(modules=modules, min_duration=0)
