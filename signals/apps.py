import logging
import os
import sys
import threading
import time

from django.apps import AppConfig
from django.conf import settings


logger = logging.getLogger(__name__)

_auto_tracking_thread_started = False


def _auto_tracking_worker():
    """Background thread: run auto-tracking check every N seconds."""
    interval = getattr(settings, "AUTO_TRACKING_BACKGROUND_INTERVAL_SECONDS", 60)
    if interval <= 0:
        return
    # Delay first run so DB and app are fully ready
    time.sleep(10)
    from django.db import connection
    from signals.auto_tracking import run_auto_tracking_check
    while True:
        try:
            run_auto_tracking_check(dry_run=False)
        except Exception as e:
            logger.exception("Auto-tracking check failed: %s", e)
        finally:
            try:
                connection.close()
            except Exception:
                pass
        time.sleep(interval)


_ibkr_connect_thread_started = False


def _ibkr_connect_worker():
    """Background thread: connect to IBKR with retry on server start."""
    import asyncio
    # ib_insync/eventkit need a current event loop in this thread (Python 3.10+)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        import nest_asyncio
        nest_asyncio.apply(loop)
    except ImportError:
        pass
    # Short delay so app and settings are ready
    time.sleep(2)
    try:
        from signals.ibkr import run_connect_and_keep_alive
        run_connect_and_keep_alive()
    except Exception as e:
        logger.exception("IBKR connect worker failed: %s", e)


def _maybe_start_auto_tracking_thread():
    """Start the auto-tracking background thread once per process (on app load)."""
    global _auto_tracking_thread_started
    if _auto_tracking_thread_started:
        return
    interval = getattr(settings, "AUTO_TRACKING_BACKGROUND_INTERVAL_SECONDS", 60)
    if interval <= 0:
        return
    # Avoid starting in runserver parent process (reloader)
    if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
        return
    _auto_tracking_thread_started = True
    t = threading.Thread(target=_auto_tracking_worker, daemon=True, name="auto-tracking")
    t.start()
    logger.info("Auto-tracking background thread started (interval=%ss).", interval)


def _maybe_start_ibkr_connect_thread():
    """Start the IBKR connect-with-retry thread once per process (on app load)."""
    global _ibkr_connect_thread_started
    if _ibkr_connect_thread_started:
        return
    if not getattr(settings, "IBKR_ENABLED", False):
        return
    # Avoid starting in runserver parent process (reloader)
    if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
        return
    _ibkr_connect_thread_started = True
    t = threading.Thread(target=_ibkr_connect_worker, daemon=True, name="ibkr-connect")
    t.start()
    logger.info("IBKR connect-with-retry thread started.")


class SignalsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'signals'

    def ready(self):
        _maybe_start_auto_tracking_thread()
        _maybe_start_ibkr_connect_thread()
