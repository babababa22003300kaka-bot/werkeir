#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚙️ Google Sheets Worker
Background worker مع 3 timers منفصلة (pending, retry, taken)
✅ محدث مع تسجيل ID History + Taken Handler
"""

import asyncio
import logging
import random
from typing import Dict

from .google_api import GoogleSheetsAPI
from .id_history import add_ids_to_history
from .logger import WeeklyLogger
from .queue_manager import (
    clear_batch,
    get_pending_batch,
    get_retry_batch,
    move_to_failed,
    move_to_retry,
    save_queue,
)

# 🆕 استيراد آمن للـ Taken Worker
try:
    from .taken import start_taken_worker

    TAKEN_WORKER_AVAILABLE = True
except ImportError:
    TAKEN_WORKER_AVAILABLE = False
    logger.warning("⚠️ Taken Worker not available - will skip")

logger = logging.getLogger(__name__)


async def pending_worker(
    config: Dict, sheets_api: GoogleSheetsAPI, weekly_log: WeeklyLogger
):
    """
    Timer 1: معالجة pending.json (1-10 ثواني)
    """
    queue_config = config.get("queue", {})
    min_interval = queue_config.get("pending_interval_min", 1)
    max_interval = queue_config.get("pending_interval_max", 10)
    max_retries = queue_config.get("max_retries", 50)

    logger.info(f"🔄 Pending worker started (interval: {min_interval}-{max_interval}s)")

    while True:
        try:
            batch = get_pending_batch()

            if batch:
                emails_data = [
                    {"email": item["email"], "id": item.get("id", "")} for item in batch
                ]

                emails = [item["email"] for item in batch]

                logger.info(f"📤 Processing {len(emails)} emails from pending queue")

                success, message = sheets_api.append_emails(emails_data)

                if success:
                    ids_to_record = [
                        item.get("id", "")
                        for item in batch
                        if item.get("id") and item.get("id") not in ["N/A", "", None]
                    ]

                    if ids_to_record:
                        add_ids_to_history(ids_to_record)

                    clear_batch("pending.json", emails)

                    log_msg = f"✅ Added {len(emails)} emails to Sheet"
                    logger.info(log_msg)
                    weekly_log.write(log_msg)

                else:
                    logger.warning(f"⚠️ Failed to add emails: {message}")

                    for item in batch:
                        attempts = item.get("attempts", 0)

                        if attempts < max_retries:
                            move_to_retry(item)
                        else:
                            move_to_failed(item)
                            log_msg = f"❌ {item['email']} moved to failed (max retries: {max_retries})"
                            logger.warning(log_msg)
                            weekly_log.write(log_msg)

                    clear_batch("pending.json", emails)

            interval = random.uniform(min_interval, max_interval)
            await asyncio.sleep(interval)

        except Exception as e:
            logger.exception(f"❌ Error in pending worker: {e}")
            await asyncio.sleep(30)


async def retry_worker(
    config: Dict, sheets_api: GoogleSheetsAPI, weekly_log: WeeklyLogger
):
    """
    Timer 2: معالجة retry.json (30-60 ثانية)
    """
    queue_config = config.get("queue", {})
    min_interval = queue_config.get("retry_interval_min", 30)
    max_interval = queue_config.get("retry_interval_max", 60)
    max_retries = queue_config.get("max_retries", 50)

    logger.info(f"🔄 Retry worker started (interval: {min_interval}-{max_interval}s)")

    while True:
        try:
            batch = get_retry_batch()

            if batch:
                emails_data = [
                    {"email": item["email"], "id": item.get("id", "")} for item in batch
                ]

                emails = [item["email"] for item in batch]

                logger.info(f"🔁 Retrying {len(emails)} emails from retry queue")

                success, message = sheets_api.append_emails(emails_data)

                if success:
                    ids_to_record = [
                        item.get("id", "")
                        for item in batch
                        if item.get("id") and item.get("id") not in ["N/A", "", None]
                    ]

                    if ids_to_record:
                        add_ids_to_history(ids_to_record)

                    clear_batch("retry.json", emails)

                    log_msg = f"✅ Added {len(emails)} emails to Sheet (retry)"
                    logger.info(log_msg)
                    weekly_log.write(log_msg)

                else:
                    logger.warning(f"⚠️ Retry failed: {message}")

                    updated_batch = []
                    failed_emails = []

                    for item in batch:
                        attempts = item.get("attempts", 0) + 1
                        item["attempts"] = attempts

                        if attempts < max_retries:
                            updated_batch.append(item)
                        else:
                            move_to_failed(item)
                            failed_emails.append(item["email"])
                            log_msg = f"❌ {item['email']} moved to failed (max retries: {max_retries})"
                            logger.warning(log_msg)
                            weekly_log.write(log_msg)

                    save_queue("retry.json", {"emails": updated_batch})

                    if failed_emails:
                        log_msg = f"❌ {len(failed_emails)} emails moved to failed"
                        weekly_log.write(log_msg)

            interval = random.uniform(min_interval, max_interval)
            await asyncio.sleep(interval)

        except Exception as e:
            logger.exception(f"❌ Error in retry worker: {e}")
            await asyncio.sleep(60)


async def start_sheet_worker(config: Dict):
    """
    تشغيل الـ Google Sheets Workers (2 أو 3 workers)

    Args:
        config: إعدادات التطبيق
    """
    try:
        sheet_config = config.get("google_sheet", {})
        credentials_file = sheet_config.get("credentials_file", "credentials.json")
        spreadsheet_id = sheet_config.get("spreadsheet_id")
        sheet_name = sheet_config.get("sheet_name", "Emails")

        if not spreadsheet_id:
            logger.error("❌ Google Sheet ID not configured!")
            return

        sheets_api = GoogleSheetsAPI(credentials_file, spreadsheet_id, sheet_name)

        log_dir = config.get("queue", {}).get("log_dir", "logs")
        weekly_log = WeeklyLogger(log_dir)

        # ✅ تحديد Workers المتاحة
        workers = [
            pending_worker(config, sheets_api, weekly_log),
            retry_worker(config, sheets_api, weekly_log),
        ]

        # ✅ إضافة Taken Worker إذا كان متوفر
        if TAKEN_WORKER_AVAILABLE:
            logger.info(
                "🚀 Starting Google Sheets workers (pending + retry + taken)..."
            )
            workers.append(start_taken_worker(config, sheets_api))
        else:
            logger.info("🚀 Starting Google Sheets workers (pending + retry only)...")

        await asyncio.gather(*workers)

    except Exception as e:
        logger.exception(f"❌ Fatal error in sheet worker: {e}")
