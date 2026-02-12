import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

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
        self.base_url = "https://api.massive.com"
        self.logger = logging.getLogger(__name__)
        # Ensure logger outputs to console if no handlers are configured
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
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
        # If it already starts with X:, it's crypto
        if ticker.startswith("X:"):
            return True
        # If it ends with USD or USDT, it's likely crypto (e.g., BTCUSD, ETHUSD)
        if ticker.endswith("USD") or ticker.endswith("USDT"):
            return True
        # Common crypto base symbols
        crypto_bases = {"BTC", "ETH", "SOL", "ADA", "DOT", "MATIC", "AVAX", "LINK", "UNI", "ATOM", "ALGO", "XRP", "DOGE", "SHIB", "LTC", "BCH", "ETC", "XLM", "AAVE", "SAND", "MANA", "AXS", "ENJ", "CHZ", "FLOW", "NEAR", "FTM", "ICP", "APT", "ARB", "OP", "SUI", "SEI", "TIA", "INJ", "RUNE", "THETA", "FIL", "EOS", "TRX", "XMR", "ZEC", "DASH", "WAVES", "ZIL", "VET", "HBAR", "IOTA", "QTUM", "ONT", "ZEN", "BAT", "OMG", "KNC", "COMP", "MKR", "SNX", "YFI", "SUSHI", "CRV", "1INCH", "BAL", "REN", "KSM", "DOT", "LUNA", "UST"}
        # Check if ticker is a crypto base symbol
        base = ticker.split("USD")[0].split("USDT")[0] if "USD" in ticker or "USDT" in ticker else ticker
        base = base.replace("X:", "").strip()
        return base in crypto_bases

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
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
        timeout: int = 10,
        max_pages: int = 5,
    ) -> Optional[list]:
        """
        Best-effort: return a list of option snapshots for an underlying using Polygon snapshot endpoint.

        Uses:
          /v3/snapshot/options/{underlying}

        Fetches pages until no more data, or until max_pages is reached (pagination limit).
        Uses expiration_date.gte and expiration_date.lte parameters to filter by expiration date.
        Optionally uses strike_price.gte and strike_price.lte when strike_gte/strike_lte are provided.

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

        import datetime as _dt
        # Parse expiration dates for filtering
        try:
            exp_gte_date = _dt.date.fromisoformat(expiration_gte)
            exp_lte_date = _dt.date.fromisoformat(expiration_lte)
        except Exception:
            exp_gte_date = None
            exp_lte_date = None

        # Try fetching with expiration filters first
        # Use a reasonable default limit per page
        default_limit = 250
        params = {
            "contract_type": s,
            "expiration_date.gte": expiration_gte,
            "expiration_date.lte": expiration_lte,
            "limit": default_limit,
        }
        if strike_gte is not None:
            params["strike_price.gte"] = strike_gte
        if strike_lte is not None:
            params["strike_price.lte"] = strike_lte

        out: list = []
        path = f"/v3/snapshot/options/{u}"
        use_expiration_filter = True
        page_count = 0

        # Fetch pages until no more data or max_pages reached
        while page_count < max_pages:
            page_count += 1
            data = self._get(path, params=params, timeout=timeout)
            
            if not data:
                # Log to console (like print) - ensure logger outputs to console
                msg = f"No data returned from Polygon API - path: {path}, params: {params}, timeout: {timeout}, underlying: {u}, side: {s}"
                self.logger.info(msg)
                # Also print directly to ensure it shows in console
                print(f"[POLYGON] {msg}")
                # If first request fails and we used expiration filters, retry without them
                if use_expiration_filter and exp_gte_date and exp_lte_date:
                    msg = f"Retrying without expiration filters (API may not support them) - underlying: {u}, side: {s}, expiration_gte: {expiration_gte}, expiration_lte: {expiration_lte}"
                    self.logger.info(msg)
                    print(f"[POLYGON] {msg}")
                    use_expiration_filter = False
                    params = {"contract_type": s, "limit": default_limit}
                    if strike_gte is not None:
                        params["strike_price.gte"] = strike_gte
                    if strike_lte is not None:
                        params["strike_price.lte"] = strike_lte
                    path = f"/v3/snapshot/options/{u}"
                    continue
                break
            
            results = data.get("results") or []
            if isinstance(results, list):
                # Filter every page by expiration and strike while paginating (no separate fetch-then-filter step)
                filtered_results = []
                for item in results:
                    details = item.get("details") or {}
                    exp_str = details.get("expiration_date") or ""
                    strike_val = self._coerce_float(details.get("strike_price"))
                    if exp_gte_date and exp_lte_date:
                        try:
                            exp_date = _dt.date.fromisoformat(exp_str)
                            if not (exp_gte_date <= exp_date <= exp_lte_date):
                                continue
                        except Exception:
                            continue
                    if strike_gte is not None and strike_val is not None and strike_val < strike_gte:
                        continue
                    if strike_lte is not None and strike_val is not None and strike_val > strike_lte:
                        continue
                    filtered_results.append(item)
                out.extend(filtered_results)
            
            # Polygon snapshot endpoints sometimes return next_url for pagination.
            next_url = data.get("next_url") or data.get("nextUrl")
            # Stop if no next_url, fewer results than limit (end of data), or hit max_pages
            current_limit = params.get("limit", default_limit) if isinstance(params, dict) else default_limit
            if not next_url or (isinstance(results, list) and len(results) < current_limit):
                break
            if page_count >= max_pages:
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
                    # If using expiration filters, preserve them in params; otherwise clear params
                    if not use_expiration_filter:
                        params = {}  # next_url already encodes query; we keep params empty to avoid collisions
                else:
                    break
            except Exception:
                break

        return out

    def get_best_option(
        self,
        *,
        underlying: str,
        side: str,
        expiration_gte: str,
        expiration_lte: str,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
        underlying_price: float,
        trade_type: str,
        timeout: int = 30,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        Fetch option chain snapshots (with pagination and expiration/strike filter) and pick the best
        contract for the given trade_type. Single entry point instead of get_option_chain_snapshots + pick_best_option_from_snapshots.

        Returns:
          (best_option_dict, fetch_failed).
          - If fetch failed (snapshots None): (None, True) -> caller should return 502.
          - If no suitable contract found: (None, False) -> caller should return 404.
          - Otherwise: (best, False).
        """
        snaps = self.get_option_chain_snapshots(
            underlying=underlying,
            side=side,
            expiration_gte=expiration_gte,
            expiration_lte=expiration_lte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
            timeout=timeout,
        )
        if snaps is None:
            return (None, True)
        best = self.pick_best_option_from_snapshots(
            snapshots=snaps,
            underlying_price=float(underlying_price),
            trade_type=trade_type,
            side=side,
        )
        return (best, False)

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
            return None

        # Helper: score moneyness (target slightly OTM within specified %).
        def moneyness_score(strike: float, is_call: bool, max_percent: float = 0.02) -> float:
            m = (strike - px) / px
            target = max_percent if is_call else -max_percent
            # Strongly prefer within max_percent, then closeness to target.
            return abs(m - target) + (0 if abs(m) <= max_percent else 0.5 + abs(m))

        # Helper: check if strike is within percentage range
        def strike_in_range(strike: float, max_percent: float) -> bool:
            m = abs((strike - px) / px)
            return m <= max_percent

        s_side = (side or "").strip().lower()
        if s_side not in ("call", "put"):
            s_side = "call"
        prefer_call = s_side == "call"

        # SCALP - Progressive Fallback
        if tt == "scalp":
            # Define fallback levels
            levels = [
                {  # LEVEL 0 (Strict)
                    "dte_range": (0, 0),
                    "delta_range": (0.35, 0.60),
                    "min_oi": 500,
                    "max_spread": 0.10,
                },
                {  # LEVEL 1 (Widen Delta)
                    "dte_range": (0, 0),
                    "delta_range": (0.25, 0.65),
                    "min_oi": 300,
                    "max_spread": 0.15,
                },
                {  # LEVEL 2 (Add 1DTE)
                    "dte_range": (0, 1),
                    "delta_range": (0.25, 0.65),
                    "min_oi": 200,
                    "max_spread": 0.15,
                },
                {  # LEVEL 3 (Add 2DTE)
                    "dte_range": (0, 2),
                    "delta_range": (0.20, 0.70),
                    "min_oi": 100,
                    "max_spread": 0.20,
                },
            ]
            
            for level_idx, level in enumerate(levels):
                candidates = []
                dte_lo, dte_hi = level["dte_range"]
                delta_lo, delta_hi = level["delta_range"]
                
                for r in rows:
                    dte = r.get("dte")
                    d = r.get("delta")
                    oi = r.get("open_interest") or 0
                    spr = r.get("spread")
                    
                    if dte is None or not (dte_lo <= dte <= dte_hi):
                        continue
                    if d is None:
                        continue
                    abs_delta = abs(d)
                    if not (delta_lo <= abs_delta <= delta_hi):
                        continue
                    if oi < level["min_oi"]:
                        continue
                    if spr is None or spr >= level["max_spread"]:
                        continue
                    candidates.append(r)
                
                if candidates:
                    # Score: closest to 0.50 delta, then shortest DTE, then tighter spread, then higher OI
                    candidates.sort(key=lambda r: (
                        abs(abs(r.get("delta") or 0) - 0.50),
                        r.get("dte") or 9999,
                        (r.get("spread") or 9e9),
                        -(r.get("open_interest") or 0)
                    ))
                    result = candidates[0]
                    result["fallback_level"] = level_idx
                    return result
            
            # All levels exhausted - abort
            return None

        # SWING - Progressive Fallback
        if tt == "swing":
            # Define fallback levels (prefer weekly 13-25, fallback to single 6-15)
            levels = [
                {  # LEVEL 0 (Strict - Try weekly first, then single)
                    "dte_ranges": [(13, 25), (6, 15)],  # Weekly first, then single
                    "delta_range": (0.40, 0.60),
                    "strike_percent": 0.02,  # ±2%
                    "min_oi": 1000,
                    "max_spread": 0.05,
                },
                {  # LEVEL 1 (Extend DTE Out)
                    "dte_ranges": [(13, 45), (6, 30)],  # Weekly first, then single
                    "delta_range": (0.40, 0.60),
                    "strike_percent": 0.02,  # ±2%
                    "min_oi": 500,
                    "max_spread": 0.08,
                },
                {  # LEVEL 2 (Extend DTE + Widen Delta & Strike)
                    "dte_ranges": [(13, 60), (6, 45)],  # Weekly first, then single
                    "delta_range": (0.30, 0.70),
                    "strike_percent": 0.05,  # ±5%
                    "min_oi": 300,
                    "max_spread": 0.10,
                },
                {  # LEVEL 3 (Maximum Flexibility - DTE Only Extends)
                    "dte_ranges": [(13, 90), (6, 60)],  # Weekly first, then single
                    "delta_range": (0.25, 0.75),
                    "strike_percent": 0.08,  # ±8%
                    "min_oi": 200,
                    "max_spread": 0.15,
                },
            ]
            
            for level_idx, level in enumerate(levels):
                candidates = []
                delta_lo, delta_hi = level["delta_range"]
                strike_percent = level["strike_percent"]
                
                for r in rows:
                    dte = r.get("dte")
                    d = r.get("delta")
                    oi = r.get("open_interest") or 0
                    spr = r.get("spread")
                    strike = r.get("strike")
                    
                    if dte is None:
                        continue
                    # Check if DTE is in any of the preferred ranges (try weekly first, then single)
                    dte_match = False
                    for dte_lo, dte_hi in level["dte_ranges"]:
                        if dte_lo <= dte <= dte_hi:
                            dte_match = True
                            break
                    if not dte_match:
                        continue
                    
                    if d is None:
                        continue
                    abs_delta = abs(d)
                    if not (delta_lo <= abs_delta <= delta_hi):
                        continue
                    
                    if strike is None or not strike_in_range(strike, strike_percent):
                        continue
                    
                    if oi < level["min_oi"]:
                        continue
                    if spr is None or spr >= level["max_spread"]:
                        continue
                    candidates.append(r)
                
                if candidates:
                    # Score: moneyness (prefer ±2% ATM, slightly OTM), then shorter DTE, then tighter spread, then higher OI
                    candidates.sort(key=lambda r: (
                        moneyness_score(r["strike"], is_call=prefer_call, max_percent=strike_percent),
                        r.get("dte") or 9999,  # Prefer shorter DTE
                        (r.get("spread") or 9e9),
                        -(r.get("open_interest") or 0)
                    ))
                    result = candidates[0]
                    result["fallback_level"] = level_idx
                    return result
            
            # All levels exhausted - abort
            return None

        # LEAP - Progressive Fallback
        if tt == "leap":
            # Define fallback levels
            levels = [
                {  # LEVEL 0 (Strict)
                    "dte_range": (330, 395),
                    "delta_range": (0.50, 0.80),
                    "strike_percent": 0.02,  # ±2%
                    "min_oi": 500,
                    "max_spread": 0.05,
                    "target_dte": 365,
                },
                {  # LEVEL 1 (Widen DTE)
                    "dte_range": (270, 450),
                    "delta_range": (0.50, 0.80),
                    "strike_percent": 0.02,  # ±2%
                    "min_oi": 300,
                    "max_spread": 0.08,
                    "target_dte": 365,
                },
                {  # LEVEL 2 (Widen Delta + Strike)
                    "dte_range": (180, 500),
                    "delta_range": (0.40, 0.85),
                    "strike_percent": 0.05,  # ±5%
                    "min_oi": 200,
                    "max_spread": 0.10,
                    "target_dte": 365,
                },
                {  # LEVEL 3 (Maximum Flexibility)
                    "dte_range": (120, 550),
                    "delta_range": (0.35, 0.90),
                    "strike_percent": 0.08,  # ±8%
                    "min_oi": 100,
                    "max_spread": 0.15,
                    "target_dte": 365,
                },
            ]
            
            for level_idx, level in enumerate(levels):
                candidates = []
                dte_lo, dte_hi = level["dte_range"]
                delta_lo, delta_hi = level["delta_range"]
                strike_percent = level["strike_percent"]
                target_dte = level.get("target_dte", 365)
                
                for r in rows:
                    dte = r.get("dte")
                    d = r.get("delta")
                    oi = r.get("open_interest") or 0
                    spr = r.get("spread")
                    strike = r.get("strike")
                    
                    if dte is None or not (dte_lo <= dte <= dte_hi):
                        continue
                    if d is None:
                        continue
                    abs_delta = abs(d)
                    if not (delta_lo <= abs_delta <= delta_hi):
                        continue
                    
                    if strike is None or not strike_in_range(strike, strike_percent):
                        continue
                    
                    if oi < level["min_oi"]:
                        continue
                    if spr is None or spr >= level["max_spread"]:
                        continue
                    candidates.append(r)
                
                if candidates:
                    # Score: closest to target DTE (365), then moneyness, then tighter spread, then higher OI
                    candidates.sort(key=lambda r: (
                        abs((r.get("dte") or 0) - target_dte),
                        moneyness_score(r["strike"], is_call=prefer_call, max_percent=strike_percent),
                        (r.get("spread") or 9e9),
                        -(r.get("open_interest") or 0)
                    ))
                    result = candidates[0]
                    result["fallback_level"] = level_idx
                    return result
            
            # All levels exhausted - abort
            return None
