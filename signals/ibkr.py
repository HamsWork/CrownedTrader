"""
Interactive Brokers (IBKR) integration for CrownedTrader.

Uses a single persistent connection (_ib_instance) for the whole system:
- Post a trade: push_position_entry() places BUY via _ib_instance
- Close trade: push_position_exit() places SELL via _ib_instance
- Sync positions: fetch_ibkr_positions() / sync_positions_from_ibkr() use _ib_instance

On server start, if IBKR_ENABLED, connects and keeps the connection alive; if
disconnected, retries until reconnected (run_connect_and_keep_alive).

Requires: TWS or IB Gateway running with API enabled (Settings -> API -> Enable).
Environment: IBKR_ENABLED=1, IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID.
"""
import asyncio
import logging
import threading
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# Single persistent IB connection for the whole system: post trade, close trade, sync positions.
# Maintained by the ibkr-connect thread. All order placement and position fetch use this when available.
_ib_instance = None
_ib_loop = None  # Event loop of the thread that owns _ib_instance (for run_coroutine_threadsafe)
_ib_lock = threading.Lock()
# When set, the keep-alive thread wakes from its retry sleep and reconnects immediately
_reconnect_requested = threading.Event()
KEEP_ALIVE_CHECK_SECONDS = 5
ORDER_TIMEOUT_SECONDS = 30
SYNC_POSITIONS_TIMEOUT_SECONDS = 15
WAIT_FOR_CONNECTION_POLL_INTERVAL = 2
# Order statuses that mean the order was rejected/cancelled (not placed correctly)
ORDER_STATUS_FAILED = frozenset({"Cancelled", "ApiCancelled", "Inactive", "Rejected"})


def _ibkr_enabled():
    return getattr(settings, "IBKR_ENABLED", False)


def _get_ib():
    try:
        from ib_insync import IB, Stock, Option, MarketOrder, LimitOrder
        return IB, Stock, Option, MarketOrder, LimitOrder
    except ImportError:
        return None



def _connect_params(client_id=None):
    """Return (host, port, client_id, timeout) from settings.
    client_id: use this if provided; else use IBKR_CLIENT_ID (for keep-alive).
    """
    base_id = getattr(settings, "IBKR_CLIENT_ID", 1)
    return (
        getattr(settings, "IBKR_HOST", "127.0.0.1"),
        getattr(settings, "IBKR_PORT", 7497),
        int(client_id) if client_id is not None else base_id,
        10,
    )


def get_ib_connection(timeout_seconds=None, poll_interval=WAIT_FOR_CONNECTION_POLL_INTERVAL):
    """Return the current persistent IB instance when connected, else None.
    If None or disconnected, requests reconnect and waits up to timeout_seconds for connection.
    """
    if timeout_seconds is None:
        timeout_seconds = getattr(settings, "IBKR_WAIT_FOR_CONNECTION_SECONDS", 30)
    deadline = time.time() + max(0, timeout_seconds)
    while time.time() < deadline:
        with _ib_lock:
            ib = _ib_instance
        if ib is not None and ib.isConnected():
            return ib
        # Ask keep-alive thread to reconnect (wakes it from retry sleep if applicable)
        _reconnect_requested.set()
        time.sleep(min(poll_interval, max(0, deadline - time.time())))
    return None


def _wait_for_connection(timeout_seconds=None, poll_interval=WAIT_FOR_CONNECTION_POLL_INTERVAL):
    """Block until get_ib_connection() and _ib_loop are ready, or timeout. Returns (ib, loop) or (None, None)."""
    ib = get_ib_connection(timeout_seconds=timeout_seconds, poll_interval=poll_interval)
    print(ib)
    with _ib_lock:
        loop = _ib_loop
    print(loop)
    if ib is not None and loop is not None:
        return ib, loop
    return None, None


def _connect_with_retry_sync():
    """Block until connected; return IB instance. Returns None if IBKR disabled or import failed."""
    if not _ibkr_enabled():
        return None
    IB = _get_ib()
    if not IB:
        return None
    IB = IB[0]
    host, port, client_id, timeout = _connect_params()
    interval = getattr(settings, "IBKR_CONNECT_RETRY_INTERVAL_SECONDS", 30)
    attempt = 0
    while True:
        attempt += 1
        try:
            ib = IB()
            ib.connect(host, port, clientId=client_id, timeout=timeout)
            logger.info("IBKR: connected successfully (attempt %s).", attempt)
            return ib
        except Exception as e:
            logger.warning("IBKR: connect attempt %s failed: %s", attempt, e)
        if attempt == 1:
            logger.info("IBKR: will retry connection every %ss until connected", interval)
        time.sleep(interval)


def _run_keep_alive_loop(ib):
    """Run the event loop until ib is disconnected. Uses periodic isConnected() check."""
    loop = asyncio.get_event_loop()

    def check_connected():
        try:
            if not ib.isConnected():
                logger.warning("IBKR: connection lost, will reconnect.")
                loop.stop()
            else:
                loop.call_later(KEEP_ALIVE_CHECK_SECONDS, check_connected)
        except Exception as e:
            logger.warning("IBKR: keep-alive check failed: %s", e)
            loop.stop()

    loop.call_later(KEEP_ALIVE_CHECK_SECONDS, check_connected)
    loop.run_forever()


def run_connect_and_keep_alive():
    """
    Connect to IBKR and keep the connection alive. If disconnected, retry until reconnected.
    Intended to run in the ibkr-connect background thread. Runs until process exit.
    Uses synchronous ib.connect(); then runs the event loop to keep the connection alive.
    """
    if not _ibkr_enabled():
        return
    IB = _get_ib()
    if not IB:
        logger.debug("IBKR: ib_insync not installed, skip connect")
        return
    interval = getattr(settings, "IBKR_CONNECT_RETRY_INTERVAL_SECONDS", 30)
    global _ib_instance, _ib_loop

    while True:
        ib = _connect_with_retry_sync()
        if ib is None:
            return
        # Use the event loop set by the ibkr-connect thread (apps.py). If missing/None, create one
        # so we never set _ib_loop to None while _ib_instance is set (avoids "Connection has no event loop").
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            logger.debug("IBKR: created event loop for keep-alive thread")
        with _ib_lock:
            _ib_instance = ib
            _ib_loop = loop
        try:
            _run_keep_alive_loop(ib)
        except Exception:
            pass
        
        logger.info("IBKR: will reconnect in %ss...", interval)
        _reconnect_requested.clear()
        _reconnect_requested.wait(timeout=interval)  # wake immediately if get_ib_connection() requested reconnect


def _position_to_contract(position):
    """Build ib_insync Contract from our Position model (for order placement)."""
    IB, Stock, Option, _, _ = _get_ib()
    if not IB:
        return None
    symbol = (position.symbol or "").strip().upper()
    if not symbol:
        return None
    if getattr(position, "instrument", None) == "shares":
        return Stock(symbol, "SMART", "USD")
    # Options
    exp = (position.expiration or "").strip().replace("-", "")  # YYYYMMDD
    strike_raw = (position.strike or "").strip()
    try:
        strike = float(strike_raw) if strike_raw else 0
    except ValueError:
        strike = 0
    right = (position.option_type or "").strip().upper()
    if right == "CALL":
        right = "C"
    elif right == "PUT":
        right = "P"
    else:
        right = "C"
    if not exp or strike <= 0:
        logger.warning("IBKR: option position missing expiration or strike: %s", position)
        return None
    return Option(symbol, exp, strike, right, "SMART", multiplier=100)


def get_display_qty(position):
    """
    Total position size in display units (for UI and exit percent).
    display_qty = quantity * multiplier.
    Same units as closed_units: shares = share count; options = contracts * 100.
    Use this for: position list display_qty, total_units, and closed_pct denominator.
    """
    q = int(position.quantity or 1)
    mult = int(getattr(position, "multiplier", None) or 100)
    return max(1, q * mult)


def _position_quantity(position):
    """
    Return IBKR order quantity: shares = share count; options = contract count.
    Must match get_display_qty: order_qty shares = display_qty; options = display_qty / multiplier.
    """
    display = get_display_qty(position)
    mult = int(getattr(position, "multiplier", None) or 100)
    if getattr(position, "instrument", None) == "shares" or mult == 1:
        return max(1, display)  # shares: display is share count
    return max(1, int(round(display / mult)))  # options: display/100 = contracts


def _display_units_to_order_qty(position, display_units):
    """
    Convert display units to IBKR order quantity.
    Display units = shares (instrument=shares) or contracts*multiplier (options).
    So: shares → order_qty = display_units; options → order_qty = display_units / multiplier.
    """
    mult = int(getattr(position, "multiplier", None) or 100)
    if getattr(position, "instrument", None) == "shares" or mult == 1:
        return max(1, int(display_units))
    return max(1, int(round(display_units / mult)))


async def _order_placed_ok(trade, symbol, side="order"):
    """After placeOrder, wait briefly and check orderStatus. Return True if accepted, False if cancelled/rejected."""
    await asyncio.sleep(2)  # Give TWS a moment to report status
    status = getattr(trade, "orderStatus", None)
    if status is None:
        return True  # no status yet, assume submitted
    st = getattr(status, "status", "") or ""
    if st in ORDER_STATUS_FAILED:
        why = getattr(status, "whyHeld", "") or getattr(status, "status", "")
        log_entries = getattr(trade, "log", []) or []
        err_msg = ""
        for e in log_entries:
            msg = getattr(e, "message", "") or getattr(e, "status", "")
            if msg and (getattr(e, "errorCode", 0) or 0) != 0:
                err_msg = msg
                break
        logger.warning("IBKR: %s for %s not placed correctly: status=%s whyHeld=%s %s", side, symbol, st, why, err_msg)
        return False
    logger.info("IBKR: %s for %s qty=%s orderId=%s status=%s", side, symbol, trade.order.totalQuantity, trade.order.orderId, st)
    return True


async def _place_entry_on_ib(ib, position):
    """Place BUY order using existing ib (must be called on the thread that owns ib). Returns True on success."""
    _, _, _, MarketOrder, LimitOrder = _get_ib()
    contract = _position_to_contract(position)
    if not contract:
        return False
    qty = _position_quantity(position)
    if qty <= 0:
        return False
    ib.qualifyContracts(contract)
    order = MarketOrder("BUY", qty)
    order.tif = "DAY"  # Avoid error 10349 (TIF set by preset); explicit DAY matches typical preset
    trade = ib.placeOrder(contract, order)
    await asyncio.sleep(1)
    if not await _order_placed_ok(trade, position.symbol, "entry"):
        return False
    return True


async def _place_exit_on_ib(ib, position, quantity_to_close, price_override):
    """Place SELL order using existing ib (must be called on the thread that owns ib). Returns True on success."""
    _, _, _, MarketOrder, LimitOrder = _get_ib()
    contract = _position_to_contract(position)
    if not contract:
        return False
    qty = max(1, int(quantity_to_close))
    ib.qualifyContracts(contract)
    order = MarketOrder("SELL", qty)
    order.tif = "DAY"  # Avoid error 10349 (TIF set by preset)
    trade = ib.placeOrder(contract, order)
    await asyncio.sleep(1)
    if not await _order_placed_ok(trade, position.symbol, "exit"):
        return False
    return True


def push_position_entry(position):
    """
    Push an entry order to IBKR for the given position (BUY).
    Uses only the single persistent _ib_instance. Waits for connection if not ready yet (e.g. server just started).
    Returns (True, None) on success, (False, error_message) on failure.
    """
    if not _ibkr_enabled():
        return False, "IBKR integration is disabled."
    _get_ib()
    ib, loop = _wait_for_connection()
    if ib is None or loop is None:
        msg = "Connection not ready. Ensure TWS or IB Gateway is running and API is enabled."
        logger.warning("IBKR: %s", msg)
        return False, msg
    try:
        future = asyncio.run_coroutine_threadsafe(
            _place_entry_on_ib(ib, position), loop
        )
        ok = future.result(timeout=ORDER_TIMEOUT_SECONDS)
        return (True, None) if ok else (False, "Order was not placed.")
    except Exception as e:
        err = str(e)
        logger.warning("IBKR: push_position_entry failed: %s", e)
        return False, err or "Unknown error"


def push_position_exit(position, quantity_to_close, price_override=None):
    """
    Push an exit (SELL/close) order to IBKR.
    Uses only the single persistent _ib_instance; no second connection.
    quantity_to_close: in IBKR order terms (shares or option contracts).
    price_override: optional limit price; if None, use market.
    Returns (True, None) on success, (False, error_message) on failure.
    """
    if not _ibkr_enabled():
        return False, "IBKR integration is disabled."
    ib, loop = _wait_for_connection()
    if ib is None or loop is None:
        msg = "Connection not ready. Ensure TWS or IB Gateway is running and API is enabled."
        logger.warning("IBKR: %s", msg)
        return False, msg
    try:
        future = asyncio.run_coroutine_threadsafe(
            _place_exit_on_ib(ib, position, quantity_to_close, price_override), loop
        )
        ok = future.result(timeout=ORDER_TIMEOUT_SECONDS)
        return (True, None) if ok else (False, "Order was not placed.")
    except Exception as e:
        err = str(e)
        logger.warning("IBKR: push_position_exit failed: %s", e)
        return False, err or "Unknown error"


async def _fetch_positions_on_ib(ib):
    """Fetch positions using existing ib (must be called on the thread that owns ib). Returns list of dicts."""
    raw = ib.positions()
    out = []
    for p in raw:
        c = p.contract
        symbol = getattr(c, "symbol", "") or ""
        asset_class = type(c).__name__
        pos_val = float(p.position)  # + long, - short
        avg_cost = float(p.avgCost) if getattr(p, "avgCost", None) is not None else 0
        account = getattr(p, "account", "") or ""
        out.append({
            "symbol": symbol,
            "asset_class": asset_class,
            "position": pos_val,
            "avgCost": avg_cost,
            "account": account,
        })
    return out


def fetch_ibkr_positions():
    """
    Fetch current positions from IBKR. Uses only the single persistent _ib_instance.
    Returns list of dicts with keys: symbol, asset_class, position, avgCost, account.
    """
    if not _ibkr_enabled():
        return []
    ib, loop = _wait_for_connection()
    if ib is None or loop is None:
        logger.warning("IBKR: persistent connection not ready after wait; cannot fetch positions.")
        return []
    try:
        future = asyncio.run_coroutine_threadsafe(
            _fetch_positions_on_ib(ib), loop
        )
        return future.result(timeout=SYNC_POSITIONS_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("IBKR: fetch_ibkr_positions failed: %s", e)
        return []


def sync_positions_from_ibkr(user=None):
    """
    Sync: fetch positions from IBKR and optionally reconcile with our Position model.
    If user is given, only consider our open positions for that user; report drift.
    Returns dict: { "ibkr_positions": list, "drift": list of str, "errors": list }.
    """
    ibkr_list = fetch_ibkr_positions()
    result = {"ibkr_positions": ibkr_list, "drift": [], "errors": []}
    if not ibkr_list:
        return result
    # Optional: load our open positions and compare by symbol/size
    try:
        from signals.models import Position
        open_qs = Position.objects.filter(status=Position.STATUS_OPEN)
        if user:
            open_qs = open_qs.filter(user=user)
        our_open = list(open_qs.values("id", "symbol", "quantity", "multiplier", "instrument"))
        for our in our_open:
            sym = (our.get("symbol") or "").strip().upper()
            if not sym:
                continue
            qty = our.get("quantity") or 1
            mult = our.get("multiplier") or 100
            # Our size: shares = qty, options = qty contracts
            our_size = qty if (our.get("instrument") == "shares") else qty
            match = [p for p in ibkr_list if (p.get("symbol") or "").upper() == sym]
            if not match:
                result["drift"].append(f"Position {sym}: in system (size {our_size}) but not in IBKR")
            else:
                ibkr_pos = sum(float(p.get("position", 0)) for p in match)
                if abs(ibkr_pos - our_size) > 0.01:
                    result["drift"].append(f"Position {sym}: system size {our_size} vs IBKR {ibkr_pos}")
    except Exception as e:
        result["errors"].append(str(e))
    return result
