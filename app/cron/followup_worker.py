#!/usr/bin/env python3
"""Follow-up email cron worker.

Runs as a one-shot script invoked by a systemd timer every 30 minutes.
Sends follow-up emails to users whose session completed ~24 hours ago.

Usage:
    python -m app.cron.followup_worker
"""

import asyncio
import sys

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.services.followup_email import send_pending_followups

logger = structlog.get_logger(__name__)


async def main() -> int:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        sent = await send_pending_followups(session)

    await engine.dispose()
    logger.info("followup_worker_complete", emails_sent=sent)
    return sent


if __name__ == "__main__":
    count = asyncio.run(main())
    sys.exit(0)
