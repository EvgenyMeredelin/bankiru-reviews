"""Parser entrypoint: configure Logfire, then run APScheduler forever.

Supports live schedule reload via **SIGHUP**.  Write the new cron values
into ``/app/.env`` inside the container, then send SIGHUP to PID 1.
The running crawl job (if any) is **not** interrupted — only the *next*
trigger time changes.

.. code-block:: bash

   # From the host — reschedule without killing a running crawl:
   docker exec bankiru-parser sh -c \\
       'printf "PARSER_CRON_HOUR=3\\nPARSER_CRON_MINUTE=30\\n" > .env && kill -HUP 1'
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bankiru.config import get_settings
from bankiru.logging import configure_logfire
from bankiru.parser.runner import run_once

# Standard library logger for this module. APScheduler and other libraries
# also log via stdlib, so using the same mechanism keeps all output consistent.
logger = logging.getLogger("bankiru.parser")

# Unique identifier for the APScheduler job. Used to reschedule the job on
# SIGHUP without creating duplicates (replace_existing=True).
JOB_ID = "bankiru_daily_crawl"

# Path to the .env file inside the container. Docker injects env vars at
# container start, but this file allows runtime overrides via `docker exec`.
_ENV_FILE = Path("/app/.env")


def _load_dotenv_into_environ() -> None:
    """Read ``/app/.env`` (if present) and patch ``os.environ``.

    Docker injects env vars at container start via the compose ``env_file``
    directive.  Those process-level vars are immutable from outside the
    container.  To let ``docker exec … > .env`` override them, we read the
    file and push its values into ``os.environ`` *before* pydantic-settings
    constructs a new ``Settings`` object (which reads ``os.environ``).
    """
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ[key] = value


def _reschedule(scheduler: AsyncIOScheduler) -> None:
    """Load ``.env`` overrides, clear cached settings, and reschedule.

    Called as a SIGHUP signal handler. The sequence is:
      1. Re-read /app/.env and push new values into os.environ
      2. Clear the lru_cache on get_settings() so the next call picks up
         the new env vars
      3. Build a new CronTrigger from the updated settings
      4. Reschedule the existing job (by JOB_ID) with the new trigger

    This allows operators to change the crawl schedule without restarting
    the container or interrupting a running crawl.
    """
    _load_dotenv_into_environ()
    # Clear the cached Settings singleton so it re-reads os.environ.
    get_settings.cache_clear()
    settings = get_settings()
    tz = ZoneInfo(settings.PARSER_TIMEZONE)

    new_trigger = CronTrigger(
        hour=settings.PARSER_CRON_HOUR,
        minute=settings.PARSER_CRON_MINUTE,
        timezone=tz,
    )
    # Reschedule the existing job — only the next trigger time changes;
    # a currently running crawl (if any) is NOT interrupted.
    scheduler.reschedule_job(JOB_ID, trigger=new_trigger)
    logger.info(
        "SIGHUP: rescheduled to %02d:%02d %s",
        settings.PARSER_CRON_HOUR, settings.PARSER_CRON_MINUTE, settings.PARSER_TIMEZONE,
    )


async def _amain() -> None:
    """Async main loop: configure the scheduler and block forever.

    The scheduler fires run_once() at the configured cron time. The process
    stays alive indefinitely (via stop_event.wait()) until killed by Docker
    or a signal. On shutdown, the scheduler is stopped gracefully.
    """
    settings = get_settings()
    tz = ZoneInfo(settings.PARSER_TIMEZONE)

    # Create the async scheduler in the parser's timezone so cron triggers
    # fire at the expected local time (e.g. 00:05 Moscow time).
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        run_once,
        CronTrigger(
            hour=settings.PARSER_CRON_HOUR,
            minute=settings.PARSER_CRON_MINUTE,
            timezone=tz,
        ),
        id=JOB_ID,
        # misfire_grace_time=3600: if the scheduler was down when the trigger
        # should have fired (e.g. container restart), still run the job if
        # less than 1 hour has passed since the missed fire time.
        misfire_grace_time=3600,
        # coalesce=True: if multiple misfires accumulated, run the job only
        # once (not once per missed trigger).
        coalesce=True,
        # max_instances=1: prevent overlapping crawl runs. If a crawl is
        # still running when the next trigger fires, skip the new trigger.
        max_instances=1,
        # replace_existing=True: if a job with this ID already exists
        # (shouldn't happen, but defensive), replace it instead of raising.
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "scheduler started: daily %02d:%02d %s",
        settings.PARSER_CRON_HOUR, settings.PARSER_CRON_MINUTE, settings.PARSER_TIMEZONE,
    )

    # Register SIGHUP handler for live schedule reload.
    # Operators can send SIGHUP to PID 1 inside the container to change
    # the cron schedule without restarting the container.
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, _reschedule, scheduler)

    # Block forever. The event is never set — the process runs until Docker
    # sends SIGTERM (container stop) or SIGKILL (force kill).
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        # Graceful shutdown: stop the scheduler without waiting for the
        # current job to finish (wait=False). Docker's stop timeout handles
        # the grace period.
        scheduler.shutdown(wait=False)


def main() -> None:
    """Top-level entrypoint called by `python -m bankiru.parser`.

    Configures Logfire observability first, then enters the async main loop.
    """
    configure_logfire(service_name="parser")
    # Set the root logger to INFO so APScheduler and httpx messages are visible.
    logging.getLogger().setLevel(logging.INFO)
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
