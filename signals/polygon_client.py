import logging
import threading
import time
from typing import Any, Dict, Optional

import requests

# In-memory TTL cache for quotes to reduce Polygon API call count (rate limits).
_quote_cache: Dict[str, tuple] = {}
_quote_cache_lock = threading.Lock()
_DEFAULT_CACHE_TTL_SEC = 30


def _quote_cache_ttl_sec() -> int:
    try:
        from django.conf import settings
        return int(getattr(settings, "POLYGON_QUOTE_CACHE_SECONDS", _DEFAULT_CACHE_TTL_SEC))
    except Exception:
        return _DEFAULT_CACHE_TTL_SEC


def _cache_get(key: str) -> Optional[Any]:
    with _quote_cache_lock:
        entry = _quote_cache.get(key)
        if entry is None:
            return None
        expiry, val = entry
        if time.time() > expiry:
            del _quote_cache[key]
            return None
        return val


def _cache_set(key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
    if ttl_sec is None:
        ttl_sec = _quote_cache_ttl_sec()
    with _quote_cache_lock:
        _quote_cache[key] = (time.time() + ttl_sec, value)


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
        """Best-effort company name for a stock or crypto ticker (empty string if unavailable)."""
        # For crypto, return a friendly name
        if self._is_crypto_symbol(ticker):
            crypto_names = {
                "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "ADA": "Cardano",
                "DOT": "Polkadot", "MATIC": "Polygon", "AVAX": "Avalanche", "LINK": "Chainlink",
                "UNI": "Uniswap", "ATOM": "Cosmos", "ALGO": "Algorand", "XRP": "Ripple",
                "DOGE": "Dogecoin", "SHIB": "Shiba Inu", "LTC": "Litecoin", "BCH": "Bitcoin Cash",
                "ETC": "Ethereum Classic", "XLM": "Stellar", "AAVE": "Aave", "SAND": "The Sandbox",
                "MANA": "Decentraland", "AXS": "Axie Infinity", "ENJ": "Enjin", "CHZ": "Chiliz",
                "FLOW": "Flow", "NEAR": "NEAR Protocol", "FTM": "Fantom", "ICP": "Internet Computer",
                "APT": "Aptos", "ARB": "Arbitrum", "OP": "Optimism", "SUI": "Sui", "SEI": "Sei",
                "TIA": "Celestia", "INJ": "Injective", "RUNE": "THORChain", "THETA": "Theta Network",
                "FIL": "Filecoin", "EOS": "EOS", "TRX": "TRON", "XMR": "Monero", "ZEC": "Zcash",
                "DASH": "Dash", "WAVES": "Waves", "ZIL": "Zilliqa", "VET": "VeChain", "HBAR": "Hedera",
                "IOTA": "IOTA", "QTUM": "Qtum", "ONT": "Ontology", "ZEN": "Horizen", "BAT": "Basic Attention Token",
                "OMG": "OMG Network", "KNC": "Kyber Network", "COMP": "Compound", "MKR": "Maker",
                "SNX": "Synthetix", "YFI": "yearn.finance", "SUSHI": "SushiSwap", "CRV": "Curve",
                "1INCH": "1inch", "BAL": "Balancer", "REN": "Ren", "KSM": "Kusama", "LUNA": "Terra",
                "UST": "TerraUSD"
            }
            base = ticker.split("USD")[0].split("USDT")[0] if "USD" in ticker or "USDT" in ticker else ticker
            base = base.replace("X:", "").strip()
            return crypto_names.get(base, f"{base} (Crypto)")
        
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

    def _is_crypto_symbol(self, ticker: str) -> bool:
        """Check if a ticker symbol is a crypto symbol."""
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return False
        # Common crypto symbols (BTC, ETH, etc.) - if it doesn't contain USD/USDT and is short, likely crypto
        # Or if it already starts with X:, it's crypto
        if ticker.startswith("X:"):
            return True
        # Common crypto base symbols
        crypto_bases = {"BTC", "ETH", "SOL", "ADA", "DOT", "MATIC", "AVAX", "LINK", "UNI", "ATOM", "ALGO", "XRP", "DOGE", "SHIB", "LTC", "BCH", "ETC", "XLM", "AAVE", "SAND", "MANA", "AXS", "ENJ", "CHZ", "FLOW", "NEAR", "FTM", "ICP", "APT", "ARB", "OP", "SUI", "SEI", "TIA", "INJ", "RUNE", "THETA", "FIL", "EOS", "TRX", "XMR", "ZEC", "DASH", "WAVES", "ZIL", "VET", "HBAR", "IOTA", "QTUM", "ONT", "ZEN", "BAT", "OMG", "KNC", "COMP", "MKR", "SNX", "YFI", "SUSHI", "CRV", "1INCH", "BAL", "REN", "KSM", "DOT", "LUNA", "UST"}
        # Check if ticker is a crypto base symbol or ends with crypto patterns
        base = ticker.split("USD")[0].split("USDT")[0] if "USD" in ticker or "USDT" in ticker else ticker
        return base in crypto_bases or len(base) <= 5  # Short symbols are often crypto

    def _normalize_crypto_ticker(self, ticker: str) -> str:
        """Normalize crypto ticker for Polygon API (X:BTCUSD format)."""
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return ticker
        # If already prefixed, return as-is
        if ticker.startswith("X:"):
            return ticker
        # Remove any existing prefix
        if ":" in ticker:
            ticker = ticker.split(":")[-1]
        # If it ends with USD/USDT, use as-is; otherwise append USD
        if ticker.endswith("USD") or ticker.endswith("USDT"):
            return f"X:{ticker}"
        return f"X:{ticker}USD"

    def get_latest_quote(self, ticker: str, bypass_cache: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get latest quote data for a ticker via /v2/last/nbbo/{ticker}.
        Results are cached for POLYGON_QUOTE_CACHE_SECONDS to reduce API calls.
        Supports both stocks and crypto (crypto symbols are prefixed with X:).

        Returns dict with:
          - p: mid price (preferred), else ask, else bid
          - bid, ask
          - source
        
        Args:
            bypass_cache: If True, bypasses cache to get fresh data (for live updates)
        """
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return None

        # Normalize crypto symbols for Polygon API
        is_crypto = self._is_crypto_symbol(ticker)
        polygon_ticker = self._normalize_crypto_ticker(ticker) if is_crypto else ticker

        cache_key = f"{'crypto' if is_crypto else 'stock'}:{ticker}"
        if not bypass_cache:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        # Try NBBO first for bid/ask (works for both stocks and crypto)
        data = self._get(f"/v2/last/nbbo/{polygon_ticker}", timeout=6)
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
                out = {"p": (bid_f + ask_f) / 2.0, "bid": bid_f, "ask": ask_f, "source": "nbbo_mid"}
                if not bypass_cache:
                    _cache_set(cache_key, out)
                return out
            if ask_f > 0:
                out = {"p": ask_f, "bid": bid_f, "ask": ask_f, "source": "nbbo_ask"}
                if not bypass_cache:
                    _cache_set(cache_key, out)
                return out
            if bid_f > 0:
                out = {"p": bid_f, "bid": bid_f, "ask": ask_f, "source": "nbbo_bid"}
                if not bypass_cache:
                    _cache_set(cache_key, out)
                return out

        # Fallback: snapshot (stocks only; crypto uses different endpoint)
        if not is_crypto:
            snap = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}", timeout=6)
        else:
            # For crypto, try snapshot endpoint
            snap = self._get(f"/v2/snapshot/locale/global/markets/crypto/tickers/{polygon_ticker}", timeout=6)
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
                out = {"p": price_f, "source": "snapshot"}
                if not bypass_cache:
                    _cache_set(cache_key, out)
                return out

        # Fallback: previous close
        prev = self.get_previous_close(ticker)
        if prev:
            close_price = prev.get("c", 0)
            try:
                close_f = float(close_price or 0)
            except Exception:
                close_f = 0.0
            if close_f > 0:
                out = {"p": close_f, "source": "previous_close"}
                if not bypass_cache:
                    _cache_set(cache_key, out)
                return out

        return None

    def get_share_current_price(self, ticker: str, bypass_cache: bool = False) -> Optional[float]:
        """Convenience: return best-effort current stock price.
        
        Args:
            bypass_cache: If True, bypasses cache to get fresh data (for live updates)
        """
        q = self.get_latest_quote(ticker, bypass_cache=bypass_cache)
        if q and q.get("p") is not None:
            try:
                return float(q["p"])
            except Exception:
                return None
        return None

    def get_last_trade(self, ticker: str, bypass_cache: bool = False) -> Optional[Dict[str, Any]]:
        """Get last trade for a ticker (stocks or options) via /v2/last/trade/{ticker}. Cached to reduce API calls.
        
        Args:
            bypass_cache: If True, bypasses cache to get fresh data (for live updates)
        """
        t = (ticker or "").strip()
        if not t:
            return None
        cache_key = f"trade:{t}"
        if not bypass_cache:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached
        data = self._get(f"/v2/last/trade/{t}", timeout=8)
        if not data or data.get("status") != "OK" or not data.get("results"):
            return None
        result = data.get("results") or None
        if result is not None and not bypass_cache:
            _cache_set(cache_key, result)
        return result

    def get_option_quote(self, contract_ticker: str, bypass_cache: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get current option quote for a Polygon options contract ticker (e.g. O:AAPL260119C00150000)
        via /v2/last/nbbo/{ticker}, with trade fallback. Results cached to reduce API calls.
        
        Args:
            bypass_cache: If True, bypasses cache to get fresh data (for live updates)
        """
        ct = (contract_ticker or "").strip()
        if not ct:
            return None

        cache_key = f"option:{ct}"
        if not bypass_cache:
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

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
            out = {
                "contract": ct,
                "bid": bid_f if bid_f > 0 else None,
                "ask": ask_f if ask_f > 0 else None,
                "mid": mid,
                "price": price,
                "timestamp": ts,
                "source": "nbbo",
            }
            if not bypass_cache:
                _cache_set(cache_key, out)
            return out

        trade = self.get_last_trade(ct, bypass_cache=bypass_cache)
        if trade and trade.get("p") is not None:
            try:
                p = float(trade.get("p"))
            except Exception:
                p = None
            if p is not None:
                out = {"contract": ct, "price": p, "timestamp": trade.get("t"), "source": "trade"}
                if not bypass_cache:
                    _cache_set(cache_key, out)
                return out

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

    def get_option_chain_snapshots(
        self,
        *,
        underlying: str,
        side: str,
        expiration_gte: str,
        expiration_lte: str,
        limit: int = 250,
        max_pages: int = 4,
        timeout: int = 10,
    ) -> Optional[list]:
        """
        Best-effort: return a list of option snapshots for an underlying using Polygon snapshot endpoint.

        Uses:
          /v3/snapshot/options/{underlying}

        We rely on snapshot data because it typically includes:
          - greeks.delta
          - open_interest
          - last_quote.bid / last_quote.ask (for spread)
          - details.strike_price / details.expiration_date / details.ticker

        Note: endpoint/fields depend on Polygon plan/entitlements; this is defensive and may return [].
        """
        u = (underlying or "").strip().upper()
        s = (side or "").strip().lower()
        if not u or s not in ("call", "put"):
            return None

        params = {
            "contract_type": s,
            "expiration_date.gte": expiration_gte,
            "expiration_date.lte": expiration_lte,
            "limit": int(limit or 250),
        }

        out: list = []
        path = f"/v3/snapshot/options/{u}"
        pages = 0
        while pages < max_pages:
            pages += 1
            data = self._get(path, params=params, timeout=timeout)
            if not data:
                break
            results = data.get("results") or []
            if isinstance(results, list):
                out.extend(results)
            # Polygon snapshot endpoints sometimes return next_url for pagination.
            next_url = data.get("next_url") or data.get("nextUrl")
            if not next_url:
                break
            # Convert next_url into a path+params call by stripping base URL if present.
            try:
                if isinstance(next_url, str) and next_url.startswith(self.base_url):
                    next_url = next_url[len(self.base_url):]
                # next_url may already include apiKey; our _get always appends apiKey, so keep only path and clear params.
                if isinstance(next_url, str) and "?" in next_url:
                    next_path = next_url.split("?", 1)[0]
                else:
                    next_path = next_url
                if isinstance(next_path, str) and next_path:
                    path = next_path
                    params = {}  # next_url already encodes query; we keep params empty to avoid collisions
                else:
                    break
            except Exception:
                break

        return out

    @staticmethod
    def _coerce_float(x: Any) -> Optional[float]:
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    def pick_best_option_from_snapshots(
        self,
        *,
        snapshots: list,
        underlying_price: float,
        trade_type: str,
        side: str = "call",
    ) -> Optional[Dict[str, Any]]:
        """
        Apply Scalp/Swing/Leap selection rules to a list of snapshot items.
        Returns normalized dict: {contract, strike, expiration, side, option_price, bid, ask, spread, delta, open_interest, dte}
        """
        if not snapshots:
            return None
        try:
            px = float(underlying_price)
        except Exception:
            return None
        if px <= 0:
            return None

        import datetime as _dt

        def _norm(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            details = item.get("details") or {}
            contract = (details.get("ticker") or item.get("ticker") or "").strip()
            exp = (details.get("expiration_date") or "").strip()
            strike = self._coerce_float(details.get("strike_price"))
            if not contract or not exp or strike is None:
                return None

            greeks = item.get("greeks") or {}
            delta = self._coerce_float(greeks.get("delta"))
            oi = item.get("open_interest")
            try:
                oi_i = int(oi) if oi is not None else None
            except Exception:
                oi_i = None

            last_quote = item.get("last_quote") or item.get("lastQuote") or {}
            bid = self._coerce_float(last_quote.get("bid"))
            ask = self._coerce_float(last_quote.get("ask"))
            spread = None
            if bid is not None and ask is not None and bid >= 0 and ask >= 0:
                spread = ask - bid

            # A usable "option_price" (mid preferred).
            option_price = None
            if bid is not None and ask is not None and bid > 0 and ask > 0:
                option_price = (bid + ask) / 2.0
            elif ask is not None and ask > 0:
                option_price = ask
            elif bid is not None and bid > 0:
                option_price = bid
            else:
                lt = item.get("last_trade") or item.get("lastTrade") or {}
                option_price = self._coerce_float(lt.get("price") or lt.get("p"))

            # DTE
            dte = None
            try:
                exp_d = _dt.date.fromisoformat(exp)
                dte = (exp_d - _dt.date.today()).days
            except Exception:
                dte = None

            return {
                "contract": contract,
                "expiration": exp,
                "strike": strike,
                "delta": delta,
                "open_interest": oi_i,
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "option_price": option_price,
                "dte": dte,
            }

        rows = []
        for it in snapshots:
            if not isinstance(it, dict):
                continue
            r = _norm(it)
            if r:
                rows.append(r)
        if not rows:
            return None

        tt = (trade_type or "").strip().lower()
        if tt not in ("scalp", "swing", "leap"):
            tt = "swing"

        # Helper: score moneyness (target slightly OTM within ~2%).
        def moneyness_score(strike: float, is_call: bool) -> float:
            m = (strike - px) / px
            target = 0.02 if is_call else -0.02
            # Strongly prefer within +/-2%, then closeness to target.
            return abs(m - target) + (0 if abs(m) <= 0.02 else 0.5 + abs(m))

        s_side = (side or "").strip().lower()
        if s_side not in ("call", "put"):
            s_side = "call"
        prefer_call = s_side == "call"

        # Scalp: Find best option by increasing DTE until one matches criteria:
        # Delta between 0.35 and 0.60, Open interest >= 500, Spread < $0.10, Closest to 0.50 delta
        # CALL for BUY signals, PUT for SELL signals
        if tt == "scalp":
            by_exp: Dict[str, list] = {}
            for r in rows:
                exp = r["expiration"]
                by_exp.setdefault(exp, []).append(r)
            
            # Sort expirations by DTE (ascending) to try increasing DTE
            exp_list = sorted(by_exp.keys(), key=lambda e: (_dt.date.fromisoformat(e) - _dt.date.today()).days)
            
            for exp in exp_list:
                candidates = []
                for r in by_exp[exp]:
                    d = r.get("delta")
                    oi = r.get("open_interest") or 0
                    spr = r.get("spread")
                    if d is None:
                        continue
                    # Delta between 0.35 and 0.60
                    abs_delta = abs(d)
                    if not (0.35 <= abs_delta <= 0.60):
                        continue
                    # Open interest >= 500
                    if oi < 500:
                        continue
                    # Spread < $0.10
                    if spr is None or spr >= 0.10:
                        continue
                    candidates.append(r)
                
                if candidates:
                    # Sort by: closest to 0.50 delta, then tighter spread, then higher OI
                    candidates.sort(key=lambda r: (
                        abs(abs(r.get("delta") or 0) - 0.50),
                        (r.get("spread") or 9e9),
                        -(r.get("open_interest") or 0)
                    ))
                    return candidates[0]

            # Fallback: pick closest-to-0.50 delta ignoring OI/spread
            rows2 = [r for r in rows if r.get("delta") is not None and 0.35 <= abs(r.get("delta")) <= 0.60]
            if not rows2:
                return None
            rows2.sort(key=lambda r: (abs(abs(r.get("delta") or 0) - 0.50), (r.get("spread") or 9e9)))
            return rows2[0]

        # Swing: Select strike within ±2% (ATM), prefer slightly OTM
        # Delta between 0.40 and 0.60
        # Expiration: 6–45 days out, prefer 13–25 DTE for weekly setups or 6–15 DTE for single timeframe
        # Open interest >= 1,000, Spread < $0.05
        if tt == "swing":
            def in_window(r, lo, hi):
                dte = r.get("dte")
                return dte is not None and lo <= dte <= hi
            
            # Filter by delta, OI, and spread first
            filtered = []
            for r in rows:
                d = r.get("delta")
                oi = r.get("open_interest") or 0
                spr = r.get("spread")
                if d is None:
                    continue
                abs_delta = abs(d)
                # Delta between 0.40 and 0.60
                if not (0.40 <= abs_delta <= 0.60):
                    continue
                # Open interest >= 1,000
                if oi < 1000:
                    continue
                # Spread < $0.05
                if spr is None or spr >= 0.05:
                    continue
                filtered.append(r)
            
            if not filtered:
                return None
            
            # Prefer DTE windows: 13-25 (weekly) or 6-15 (single timeframe), then 6-45
            window_rows = [r for r in filtered if in_window(r, 13, 25)]
            if not window_rows:
                window_rows = [r for r in filtered if in_window(r, 6, 15)]
            if not window_rows:
                window_rows = [r for r in filtered if in_window(r, 6, 45)]
            if not window_rows:
                window_rows = filtered
            
            # Sort by: moneyness (prefer ±2% ATM, slightly OTM), then tighter spread, then higher OI
            window_rows.sort(
                key=lambda r: (
                    moneyness_score(r["strike"], is_call=prefer_call),
                    (r.get("spread") or 9e9),
                    -(r.get("open_interest") or 0),
                )
            )
            return window_rows[0]

        # LEAP: Select strike within ±2% (ATM), prefer slightly OTM
        # Delta between 0.50 and 0.80 (higher delta for LEAPs)
        # Expiration: 330–395 days out (~1 year), target closest to 365 DTE
        # Open interest >= 500, Spread < $0.05
        if tt == "leap":
            def in_window(r, lo, hi):
                dte = r.get("dte")
                return dte is not None and lo <= dte <= hi
            
            # Filter by delta, OI, and spread first
            filtered = []
            for r in rows:
                d = r.get("delta")
                oi = r.get("open_interest") or 0
                spr = r.get("spread")
                if d is None:
                    continue
                abs_delta = abs(d)
                # Delta between 0.50 and 0.80
                if not (0.50 <= abs_delta <= 0.80):
                    continue
                # Open interest >= 500
                if oi < 500:
                    continue
                # Spread < $0.05
                if spr is None or spr >= 0.05:
                    continue
                filtered.append(r)
            
            if not filtered:
                return None
            
            # Prefer 330-395 DTE window, target closest to 365 DTE
            window_rows = [r for r in filtered if in_window(r, 330, 395)]
            if not window_rows:
                window_rows = filtered
            
            # Sort by: closest to 365 DTE, then moneyness, then tighter spread, then higher OI
            window_rows.sort(
                key=lambda r: (
                    abs((r.get("dte") or 0) - 365),  # Closest to 365 DTE
                    moneyness_score(r["strike"], is_call=prefer_call),
                    (r.get("spread") or 9e9),
                    -(r.get("open_interest") or 0),
                )
            )
            return window_rows[0]
