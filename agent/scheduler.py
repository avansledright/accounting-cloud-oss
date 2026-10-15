"""Background scheduler for generating financial reports on a cron-like schedule."""

import logging
import os
import threading
from datetime import datetime, timedelta

from agent.db import db
from agent.reports import (
    generate_weekly_report, generate_monthly_report, generate_portfolio_report,
    _week_key, _month_key,
)

logger = logging.getLogger(__name__)


def _check_and_generate():
    """Check if current period reports exist; generate if missing."""
    now = datetime.now()

    # Weekly report
    week = _week_key(now)
    if not db.reports.find_one({'type': 'weekly', 'period': week}):
        logger.info("Missing weekly report for %s, generating...", week)
        try:
            generate_weekly_report()
        except Exception:
            logger.exception("Failed to generate weekly report for %s", week)

    # Monthly report
    month = _month_key(now)
    if not db.reports.find_one({'type': 'monthly', 'period': month}):
        logger.info("Missing monthly report for %s, generating...", month)
        try:
            generate_monthly_report()
        except Exception:
            logger.exception("Failed to generate monthly report for %s", month)

    # Portfolio report (monthly cadence, separate from the household monthly review)
    if not db.reports.find_one({'type': 'portfolio', 'period': month}):
        logger.info("Missing portfolio report for %s, generating...", month)
        try:
            generate_portfolio_report()
        except Exception:
            logger.exception("Failed to generate portfolio report for %s", month)


def schedule_reports():
    """Start a daemon thread that generates reports on schedule.

    - Weekly: every Monday at 6am
    - Monthly: 1st of each month at 6am
    - On startup: generates any missing current-period reports

    Disabled unless ADVISOR_ENABLED=true, so a fresh install without an LLM
    doesn't fill the reports collection with insight-less entries. Reports
    can still be generated on demand from the Reports page.
    """
    if os.environ.get('ADVISOR_ENABLED', 'false').lower() != 'true':
        logger.info("Report scheduler disabled (set ADVISOR_ENABLED=true to enable)")
        return

    def report_loop():
        # Startup catch-up
        try:
            _check_and_generate()
        except Exception:
            logger.exception("Startup report generation failed")

        while True:
            now = datetime.now()

            # Calculate next trigger: either next Monday 6am or next 1st 6am,
            # whichever comes first
            next_monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
            next_monday = next_monday.replace(hour=6, minute=0, second=0, microsecond=0)
            if now.weekday() == 0 and now.hour < 6:
                next_monday = now.replace(hour=6, minute=0, second=0, microsecond=0)

            if now.day == 1 and now.hour < 6:
                next_first = now.replace(hour=6, minute=0, second=0, microsecond=0)
            elif now.month == 12:
                next_first = now.replace(year=now.year + 1, month=1, day=1,
                                         hour=6, minute=0, second=0, microsecond=0)
            else:
                next_first = now.replace(month=now.month + 1, day=1,
                                         hour=6, minute=0, second=0, microsecond=0)

            next_wake = min(next_monday, next_first)
            wait_seconds = (next_wake - now).total_seconds()
            logger.info("Report scheduler sleeping %.0f seconds until %s", wait_seconds, next_wake)

            threading.Event().wait(wait_seconds)

            try:
                _check_and_generate()
            except Exception:
                logger.exception("Scheduled report generation failed")

    t = threading.Thread(target=report_loop, daemon=True)
    t.start()
    logger.info("Report scheduler started")
