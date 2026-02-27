"""
Shared logic for automatic position tracking (TP/SL).
Used by the management command and by the background thread started on app load.
"""
import logging
from django.db import transaction

from signals.models import Position
from signals.views import _get_position_current_price, _apply_position_exit


logger = logging.getLogger(__name__)


def _to_float(v):
    try:
        s = str(v or "").strip().replace("%", "")
        return float(s) if s else 0.0
    except (TypeError, ValueError):
        return 0.0


def run_auto_tracking_check(dry_run=False):
    """
    Check all open positions with mode=auto; post TP/SL exit when price hits level.
    Also handles trailing stops.
    dry_run: if True, do not send Discord or update DB.
    """
    open_auto = Position.objects.filter(
        status=Position.STATUS_OPEN,
        mode=Position.MODE_AUTO,
    ).select_related("signal")

    for pos in open_auto:
        data = (
            pos.signal.data
            if (pos.signal and isinstance(getattr(pos.signal, "data", None), dict))
            else {}
        ) or {}
        sl_price = _to_float(data.get("sl_price"))
        next_tp = (pos.tp_hit_level or 0) + 1
        next_tp_price = _to_float(data.get(f"tp{next_tp}_price"))

        current_price = _get_position_current_price(pos)
        if current_price is None:
            logger.debug(
                "check_auto_positions: position id=%s symbol=%s no quote, skip",
                pos.id,
                pos.symbol,
            )
            continue

        # Check trailing stop if configured
        trailing_stop_trigger = str(data.get("trailing_stop_trigger", "")).strip().lower()
        trailing_stop_per = _to_float(data.get("trailing_stop_per", ""))
        
        trailing_stop_active = False
        trailing_stop_price = None
        
        if trailing_stop_trigger and trailing_stop_trigger != "none" and trailing_stop_per > 0:
            # Determine if trailing stop should be active based on trigger condition
            if trailing_stop_trigger == "entry":
                # Trailing stop activates immediately after entry
                trailing_stop_active = True
            elif trailing_stop_trigger.startswith("tp"):
                # Trailing stop activates when a specific TP level is hit
                try:
                    trigger_tp_level = int(trailing_stop_trigger.replace("tp", ""))
                    # Trailing stop activates when TP level is hit (tp_hit_level >= trigger_tp_level)
                    trailing_stop_active = (pos.tp_hit_level or 0) >= trigger_tp_level
                except (ValueError, TypeError):
                    trailing_stop_active = False
            
            if trailing_stop_active:
                # Calculate trailing stop price: highest price * (1 - trailing_stop_per/100)
                # Track and persist the highest price seen on the Position for correct trailing behavior.
                highest_price = float(pos.highest_price) if getattr(pos, "highest_price", None) is not None else current_price
                if current_price > highest_price:
                    highest_price = current_price
                    if not dry_run:
                        # Persist new peak so future checks use it even after restart.
                        pos.highest_price = highest_price
                        pos.save(update_fields=["highest_price"])
                trailing_stop_price = highest_price * (1 - trailing_stop_per / 100)
                
                # Check if price has dropped below trailing stop
                if current_price <= trailing_stop_price:
                    if dry_run:
                        logger.info(
                            "Would post trailing stop exit for position id=%s %s (current=%.2f <= trailing_stop=%.2f)",
                            pos.id, pos.symbol, current_price, trailing_stop_price,
                        )
                        continue
                    with transaction.atomic():
                        pos.refresh_from_db()
                        if pos.status != Position.STATUS_OPEN:
                            continue
                        if _apply_position_exit(pos, "sl"):  # Use "sl" exit type for trailing stop
                            logger.info(
                                "check_auto_positions: posted trailing stop exit position_id=%s symbol=%s",
                                pos.id,
                                pos.symbol,
                            )
                        else:
                            logger.warning(
                                "check_auto_positions: failed to post trailing stop exit position_id=%s",
                                pos.id,
                            )
                    continue

        # Stop loss: price at or past stop level
        if sl_price > 0 and current_price <= sl_price:
            if dry_run:
                logger.info(
                    "Would post SL exit for position id=%s %s (current=%.2f <= sl=%.2f)",
                    pos.id, pos.symbol, current_price, sl_price,
                )
                continue
            with transaction.atomic():
                pos.refresh_from_db()
                if pos.status != Position.STATUS_OPEN:
                    continue
                if _apply_position_exit(pos, "sl"):
                    logger.info(
                        "check_auto_positions: posted SL exit position_id=%s symbol=%s",
                        pos.id,
                        pos.symbol,
                    )
                else:
                    logger.warning(
                        "check_auto_positions: failed to post SL exit position_id=%s",
                        pos.id,
                    )
            continue

        # Take profit: price at or past next TP level
        if next_tp_price > 0 and current_price >= next_tp_price:
            if dry_run:
                logger.info(
                    "Would post TP%d exit for position id=%s %s (current=%.2f >= tp=%.2f)",
                    next_tp, pos.id, pos.symbol, current_price, next_tp_price,
                )
                continue
            with transaction.atomic():
                pos.refresh_from_db()
                if pos.status != Position.STATUS_OPEN:
                    continue
                if _apply_position_exit(pos, "tp"):
                    logger.info(
                        "check_auto_positions: posted TP%d exit position_id=%s symbol=%s",
                        next_tp,
                        pos.id,
                        pos.symbol,
                    )
                else:
                    logger.warning(
                        "check_auto_positions: failed to post TP exit position_id=%s",
                        pos.id,
                    )
