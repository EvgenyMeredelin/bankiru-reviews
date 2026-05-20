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

logger = logging.getLogger("bankiru.parser")

JOB_ID = "bankiru_daily_crawl"
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
    """Load ``.env`` overrides, clear cached settings, and reschedule."""
    _load_dotenv_into_environ()
    get_settings.cache_clear()
    settings = get_settings()
    tz = ZoneInfo(settings.PARSER_TIMEZONE)

    new_trigger = CronTrigger(
        hour=settings.PARSER_CRON_HOUR,
        minute=settings.PARSER_CRON_MINUTE,
        timezone=tz,
    )
    scheduler.reschedule_job(JOB_ID, trigger=new_trigger)
    logger.info(
        "SIGHUP: rescheduled to %02d:%02d %s",
        settings.PARSER_CRON_HOUR, settings.PARSER_CRON_MINUTE, settings.PARSER_TIMEZONE,
    )


async def _amain() -> None:
    settings = get_settings()
    tz = ZoneInfo(settings.PARSER_TIMEZONE)

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        run_once,
        CronTrigger(
            hour=settings.PARSER_CRON_HOUR,
            minute=settings.PARSER_CRON_MINUTE,
            timezone=tz,
        ),
        id=JOB_ID,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "scheduler started: daily %02d:%02d %s",
        settings.PARSER_CRON_HOUR, settings.PARSER_CRON_MINUTE, settings.PARSER_TIMEZONE,
    )

    # Register SIGHUP handler for live schedule reload.
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, _reschedule, scheduler)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)


def main() -> None:
    configure_logfire(service_name="parser")
    logging.getLogger().setLevel(logging.INFO)
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
