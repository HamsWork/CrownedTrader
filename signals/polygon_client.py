import logging
from typing import Any, Dict, Optional

import requests


class PolygonClient:
    """
    Minimal Polygon.io client for stock quotes used by the dashboard.

    Modeled after your AI-Trader `bot/polygon_client.py`, but synchronous (Django views).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key or ""
        self.base_url = "https://api.polygon.io"
        self.logger = logging.getLogger(__name__)
        self.last_error: Optional[Dict[str, Any]] = None

    def _get(self, path: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 6) -> Optional[Dict[str, Any]]:
        if not self.api_key:
            self.last_error = {"kind": "missing_api_key"}
            return None
        url = f"{self.base_url}{path}"
        p = dict(params or {})
        p["apiKey"] = self.api_key
        try:
            resp = requests.get(url, params=p, timeout=timeout)
            if resp.status_code != 200:
                # Keep a small snippet for debugging (avoid huge payloads).
                body = ""
                try:
                    body = (resp.text or "")[:300]
                except Exception:
                    body = ""
                self.last_error = {"kind": "http_error", "status": resp.status_code, "url": url, "body": body}
                return None
            self.last_error = None
            return resp.json() if resp.content else None
        except requests.RequestException:
            self.last_error = {"kind": "network_error", "url": url}
            return None

    def get_ticker_details(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get ticker details (company name, etc.)."""
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return None
        data = self._get(f"/v3/reference/tickers/{ticker}")
        if not data or data.get("status") != "OK":
            return None
        return data.get("results") or None

    def get_company_name(self, ticker: str) -> str:
        """Best-effort company name for a stock ticker (empty string if unavailable)."""
        details = self.get_ticker_details(ticker)
        if not details:
            return ""
        name = details.get("name")
        return str(name).strip() if name else ""

    def get_previous_close(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get previous day's close bar from /v2/aggs/ticker/{ticker}/prev."""
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return None
        data = self._get(f"/v2/aggs/ticker/{ticker}/prev", timeout=8)
        if not data or data.get("status") != "OK":
            return None
        results = data.get("results") or []
        return results[0] if results else None

    def get_latest_quote(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Get latest quote data for a ticker via /v2/last/nbbo/{ticker}.

        Returns dict with:
          - p: mid price (preferred), else ask, else bid
          - bid, ask
          - source
        """
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return None

        # Try NBBO first for bid/ask
        data = self._get(f"/v2/last/nbbo/{ticker}", timeout=6)
        if data and data.get("status") == "OK" and data.get("results"):
            results = data["results"] or {}
            bid = results.get("p", 0)  # bid price
            ask = results.get("P", 0)  # ask price

            try:
                bid_f = float(bid or 0)
                ask_f = float(ask or 0)
            except Exception:
                bid_f = 0.0
                ask_f = 0.0

            if bid_f > 0 and ask_f > 0:
                return {"p": (bid_f + ask_f) / 2.0, "bid": bid_f, "ask": ask_f, "source": "nbbo_mid"}
            if ask_f > 0:
                return {"p": ask_f, "bid": bid_f, "ask": ask_f, "source": "nbbo_ask"}
            if bid_f > 0:
                return {"p": bid_f, "bid": bid_f, "ask": ask_f, "source": "nbbo_bid"}

        # Fallback: snapshot (often available even when NBBO isn't entitled)
        snap = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}", timeout=6)
        if snap and snap.get("ticker"):
            t = snap.get("ticker") or {}
            last_trade = t.get("lastTrade") or {}
            day = t.get("day") or {}
            prev_day = t.get("prevDay") or {}
            price = last_trade.get("p")
            if price is None:
                price = day.get("c")
            if price is None:
                price = prev_day.get("c")
            try:
                price_f = float(price) if price is not None else 0.0
            except Exception:
                price_f = 0.0
            if price_f > 0:
                return {"p": price_f, "source": "snapshot"}

        # Fallback: previous close
        prev = self.get_previous_close(ticker)
        if prev:
            close_price = prev.get("c", 0)
            try:
                close_f = float(close_price or 0)
            except Exception:
                close_f = 0.0
            if close_f > 0:
                return {"p": close_f, "source": "previous_close"}

        return None

    def get_share_current_price(self, ticker: str) -> Optional[float]:
        """Convenience: return best-effort current stock price."""
        q = self.get_latest_quote(ticker)
        if q and q.get("p") is not None:
            try:
                return float(q["p"])
            except Exception:
                return None
        return None

    def get_last_trade(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get last trade for a ticker (stocks or options) via /v2/last/trade/{ticker}."""
        t = (ticker or "").strip()
        if not t:
            return None
        data = self._get(f"/v2/last/trade/{t}", timeout=8)
        if not data or data.get("status") != "OK" or not data.get("results"):
            return None
        return data.get("results") or None

    def get_option_quote(self, contract_ticker: str) -> Optional[Dict[str, Any]]:
        """
        Get current option quote for a Polygon options contract ticker (e.g. O:AAPL260119C00150000)
        via /v2/last/nbbo/{ticker}, with trade fallback.
        """
        ct = (contract_ticker or "").strip()
        if not ct:
            return None

        data = self._get(f"/v2/last/nbbo/{ct}", timeout=8)
        if data and data.get("status") == "OK" and data.get("results"):
            results = data["results"] or {}
            bid = results.get("p", 0)
            ask = results.get("P", 0)
            ts = results.get("t")
            try:
                bid_f = float(bid or 0)
                ask_f = float(ask or 0)
            except Exception:
                bid_f = 0.0
                ask_f = 0.0

            mid = None
            if bid_f > 0 and ask_f > 0:
                mid = (bid_f + ask_f) / 2.0
            price = mid if mid is not None else (ask_f if ask_f > 0 else (bid_f if bid_f > 0 else None))
            return {
                "contract": ct,
                "bid": bid_f if bid_f > 0 else None,
                "ask": ask_f if ask_f > 0 else None,
                "mid": mid,
                "price": price,
                "timestamp": ts,
                "source": "nbbo",
            }

        trade = self.get_last_trade(ct)
        if trade and trade.get("p") is not None:
            try:
                p = float(trade.get("p"))
            except Exception:
                p = None
            if p is not None:
                return {"contract": ct, "price": p, "timestamp": trade.get("t"), "source": "trade"}

        return None

    def find_nearest_option_contract(
        self,
        *,
        underlying: str,
        expiration: str,
        side: str,
        target_strike: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Find the nearest listed options contract (by strike) for an underlying/expiration/side.

        Uses Polygon reference endpoint:
          /v3/reference/options/contracts
        Returns: { "contract": "O:...", "strike": float }
        """
        u = (underlying or "").strip().upper()
        exp = (expiration or "").strip()
        s = (side or "").strip().lower()
        if not u or not exp or s not in ("call", "put"):
            return None
        try:
            k = float(target_strike)
        except Exception:
            return None

        def _pick_first(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if not data or data.get("status") != "OK":
                return None
            results = data.get("results") or []
            return results[0] if results else None

        base = {
            "underlying_ticker": u,
            "expiration_date": exp,
            "contract_type": s,
            "limit": 1,
            "sort": "strike_price",
        }

        # Nearest above/equal
        above = self._get(
            "/v3/reference/options/contracts",
            params={**base, "order": "asc", "strike_price.gte": k},
            timeout=10,
        )
        a0 = _pick_first(above)

        # Nearest below/equal
        below = self._get(
            "/v3/reference/options/contracts",
            params={**base, "order": "desc", "strike_price.lte": k},
            timeout=10,
        )
        b0 = _pick_first(below)

        best = None
        if a0 and a0.get("ticker") and a0.get("strike_price") is not None:
            best = a0
        if b0 and b0.get("ticker") and b0.get("strike_price") is not None:
            if not best:
                best = b0
            else:
                try:
                    da = abs(float(best.get("strike_price")) - k)
                    db = abs(float(b0.get("strike_price")) - k)
                    if db < da:
                        best = b0
                except Exception:
                    pass

        if not best:
            return None

        try:
            strike_val = float(best.get("strike_price"))
        except Exception:
            strike_val = None

        return {
            "contract": best.get("ticker"),
            "strike": strike_val,
        }
