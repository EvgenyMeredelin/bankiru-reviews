"""Logfire setup helpers shared between API and parser services.

Logfire (https://logfire.pydantic.dev/) is the observability backend used by
this project. It provides structured logging, distributed tracing, and metrics
via an OpenTelemetry-compatible collector.

Every runnable service calls these two functions at startup:
  - configure_logfire("api")       — in bankiru/api/__main__.py
  - configure_logfire("parser")    — in bankiru/parser/__main__.py
  - configure_logfire("embedder")  — in bankiru/embedder/__main__.py
  - configure_logfire("ui")        — in bankiru/ui/__main__.py

The LOGFIRE_TOKEN env var (optional) controls where traces are sent.
When the token is None, Logfire falls back to local/anonymous mode,
which is useful for development without a remote collector.

Auto-tracing (install_auto_tracing) instruments every function call in the
specified modules with zero-code-change spans, giving full call-tree
visibility in the Logfire dashboard.
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
    logfire.configure(service_name=service_name)
    logging.basicConfig(handlers=[logfire.LogfireLoggingHandler()])


def install_auto_tracing(modules: list[str]) -> None:
    """Auto-trace the given modules with no minimum duration.

    Instruments every function/method in the listed module paths (e.g.
    ``["bankiru.api", "bankiru.parser"]``) so that each call automatically
    creates an OpenTelemetry span. ``min_duration=0`` means even fast
    functions are traced — useful for debugging but may increase trace
    volume in production. Adjust if trace costs become a concern.

    Called at service startup alongside configure_logfire(). Each service
    traces only its own module subtree to avoid redundant instrumentation.
    """
    logfire.install_auto_tracing(modules=modules, min_duration=0)
