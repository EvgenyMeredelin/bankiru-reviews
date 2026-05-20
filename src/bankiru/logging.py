"""Logfire setup helpers shared between API and parser services."""

from __future__ import annotations

import logging

import logfire


def configure_logfire(service_name: str) -> None:
    """Configure Logfire for the current process."""
    logfire.configure(service_name=service_name)
    logging.basicConfig(handlers=[logfire.LogfireLoggingHandler()])


def install_auto_tracing(modules: list[str]) -> None:
    """Auto-trace the given modules with no minimum duration."""
    logfire.install_auto_tracing(modules=modules, min_duration=0)
