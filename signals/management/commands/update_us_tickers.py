import csv
import io
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

import requests
from django.conf import settings
from django.core.management.base import BaseCommand


NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

TRADINGVIEW_DEFAULT_MARKET = "america"
TRADINGVIEW_DEFAULT_EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]


def _read_symbol_dir(text: str):
    """
    NASDAQ Trader symbol directory files are pipe-delimited.
    Last line is a footer like: "File Creation Time: ..."
    """
    lines = text.splitlines()
    if not lines:
        return [], []
    # Drop footer line if it doesn't look like a header/data row
    if lines[-1].lower().startswith("file creation time"):
        lines = lines[:-1]
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter="|")
    return list(reader), reader.fieldnames or []


def _coerce_bool_flag(val: str) -> bool:
    return str(val or "").strip().upper() in ("Y", "YES", "TRUE", "1")


def _tv_scan_url(market: str) -> str:
    market = (market or "").strip().lower() or TRADINGVIEW_DEFAULT_MARKET
    return f"https://scanner.tradingview.com/{market}/scan"


def _tv_headers():
    # Light spoofing to reduce 4xx/blocked responses.
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    }


def _tv_fetch_exchange_symbols(
    session: requests.Session,
    *,
    market: str,
    exchange: str,
    instrument_type: str,
    include_etfs: bool,
    exclude_etfs: bool,
    batch_size: int = 1000,
    max_rows: Optional[int] = None,
):
    """
    Fetch symbols for a single exchange via TradingView's scanner endpoint.

    Response items look like:
      {"s": "NASDAQ:AAPL", "d": ["AAPL", "Apple Inc.", "stock", "NASDAQ", ...]}
    """
    url = _tv_scan_url(market)
    exchange = (exchange or "").strip().upper()
    if not exchange:
        return {}

    # Determine allowed instrument types.
    # TradingView uses lower-case strings like "stock", "etf".
    instrument_type = (instrument_type or "").strip().lower()
    if instrument_type not in ("stock", "etf"):
        return {}

    columns = [
        "name",  # short symbol (often matches the right side of s=EXCHANGE:SYMBOL)
        "description",  # company/ETF name
        "type",
        "exchange",
    ]

    tickers: dict[str, str] = {}
    start = 0
    seen_total = None

    while True:
        if max_rows is not None and start >= max_rows:
            break

        end = start + batch_size
        if max_rows is not None:
            end = min(end, max_rows)

        payload = {
            "filter": [
                {"left": "exchange", "operation": "equal", "right": exchange},
                {"left": "type", "operation": "equal", "right": instrument_type},
                # Exclude obvious test / odd symbols (best-effort; TV varies).
                {"left": "name", "operation": "nempty"},
            ],
            "columns": columns,
            "sort": {"sortBy": "name", "sortOrder": "asc"},
            "range": [start, end],
        }

        resp = session.post(url, headers=_tv_headers(), json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}

        if seen_total is None:
            seen_total = data.get("totalCount")

        rows = data.get("data") or []
        if not rows:
            break

        for row in rows:
            s = (row.get("s") or "").strip()
            d = row.get("d") or []
            # Symbol is best taken from "s" which includes exchange prefix.
            sym = ""
            if ":" in s:
                sym = s.split(":", 1)[1].strip().upper()
            elif d and isinstance(d[0], str):
                sym = d[0].strip().upper()

            if not sym:
                continue

            # Description is typically d[1] given our columns, but be defensive.
            name = ""
            if len(d) >= 2 and isinstance(d[1], str):
                name = d[1].strip()
            elif len(d) >= 1 and isinstance(d[0], str):
                name = d[0].strip()

            # Basic sanity filter: US tickers are mostly alnum plus . and -
            # Keep BRK.B, BF.B, etc.
            # Avoid obviously malformed strings.
            if not all(ch.isalnum() or ch in ".-" for ch in sym):
                continue

            if sym not in tickers or (not tickers[sym] and name):
                tickers[sym] = name

        start = end
        # Stop if we have a totalCount and we reached/passed it.
        if isinstance(seen_total, int) and start >= seen_total:
            break

    return tickers


def _download_us_tickers_from_tradingview(
    *,
    market: str,
    exchanges: List[str],
    include_etfs: bool,
    exclude_etfs: bool,
    max_rows_per_exchange: Optional[int] = None,
):
    session = requests.Session()
    combined: Dict[str, str] = {}

    types_to_fetch = ["stock"]
    if not exclude_etfs:
        types_to_fetch.append("etf")
    if include_etfs and "etf" not in types_to_fetch:
        types_to_fetch.append("etf")

    for ex in exchanges:
        for instrument_type in types_to_fetch:
            per_ex = _tv_fetch_exchange_symbols(
                session,
                market=market,
                exchange=ex,
                instrument_type=instrument_type,
                include_etfs=include_etfs,
                exclude_etfs=exclude_etfs,
                max_rows=max_rows_per_exchange,
            )
            # Merge; prefer non-empty names.
            for sym, name in per_ex.items():
                if sym not in combined or (not combined[sym] and name):
                    combined[sym] = name

    return combined


class Command(BaseCommand):
    help = "Download and cache US ticker symbols list (NASDAQ Trader or TradingView)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default=os.path.join(settings.BASE_DIR, "signals", "data", "us_tickers.json"),
            help="Output JSON path (default: signals/data/us_tickers.json)",
        )
        parser.add_argument(
            "--source",
            choices=["nasdaqtrader", "tradingview"],
            default="nasdaqtrader",
            help="Data source to use (default: nasdaqtrader).",
        )
        parser.add_argument(
            "--tv-market",
            default=TRADINGVIEW_DEFAULT_MARKET,
            help="TradingView scanner market (default: america).",
        )
        parser.add_argument(
            "--tv-exchanges",
            default=",".join(TRADINGVIEW_DEFAULT_EXCHANGES),
            help="Comma-separated TradingView exchanges to scan (default: NASDAQ,NYSE,AMEX).",
        )
        parser.add_argument(
            "--tv-max-rows-per-exchange",
            type=int,
            default=0,
            help="Optional safety cap per exchange for TradingView scanning (0 = no cap).",
        )
        parser.add_argument(
            "--include-etfs",
            action="store_true",
            help="Include ETFs (default: included).",
        )
        parser.add_argument(
            "--exclude-etfs",
            action="store_true",
            help="Exclude ETFs.",
        )

    def handle(self, *args, **options):
        out_path = options["out"]
        exclude_etfs = bool(options.get("exclude_etfs"))
        include_etfs = bool(options.get("include_etfs"))
        if include_etfs and exclude_etfs:
            raise SystemExit("Choose only one of --include-etfs or --exclude-etfs")

        source = options.get("source") or "nasdaqtrader"
        tickers: dict[str, str] = {}

        if source == "tradingview":
            market = options.get("tv_market") or TRADINGVIEW_DEFAULT_MARKET
            exchanges_raw = options.get("tv_exchanges") or ""
            exchanges = [e.strip().upper() for e in exchanges_raw.split(",") if e.strip()]
            max_rows_per_exchange = int(options.get("tv_max_rows_per_exchange") or 0) or None

            self.stdout.write(
                f"Downloading symbols from TradingView (market={market}, exchanges={exchanges})..."
            )
            tickers = _download_us_tickers_from_tradingview(
                market=market,
                exchanges=exchanges or TRADINGVIEW_DEFAULT_EXCHANGES,
                include_etfs=include_etfs,
                exclude_etfs=exclude_etfs,
                max_rows_per_exchange=max_rows_per_exchange,
            )
            self.stdout.write(f"TradingView symbols: {len(tickers)}")
        else:
            self.stdout.write("Downloading symbol directories from NASDAQ Trader...")
            nasdaq_text = requests.get(NASDAQ_LISTED_URL, timeout=30).text
            other_text = requests.get(OTHER_LISTED_URL, timeout=30).text

            nasdaq_rows, nasdaq_fields = _read_symbol_dir(nasdaq_text)
            other_rows, other_fields = _read_symbol_dir(other_text)

            self.stdout.write(f"NASDAQ rows: {len(nasdaq_rows)} fields: {nasdaq_fields}")
            self.stdout.write(f"OTHER rows:  {len(other_rows)} fields: {other_fields}")

            # NASDAQ listed file
            for row in nasdaq_rows:
                sym = (row.get("Symbol") or "").strip().upper()
                name = (row.get("Security Name") or "").strip()
                if not sym:
                    continue
                if _coerce_bool_flag(row.get("Test Issue")):
                    continue
                # Optional ETF filtering
                is_etf = _coerce_bool_flag(row.get("ETF"))
                if exclude_etfs and is_etf:
                    continue
                tickers[sym] = name

            # Other listed file (NYSE/AMEX/etc)
            for row in other_rows:
                sym = (row.get("ACT Symbol") or row.get("Symbol") or "").strip().upper()
                name = (row.get("Security Name") or "").strip()
                if not sym:
                    continue
                if _coerce_bool_flag(row.get("Test Issue")):
                    continue
                # ETF column might exist as "ETF" depending on file version
                is_etf = _coerce_bool_flag(row.get("ETF"))
                if exclude_etfs and is_etf:
                    continue
                tickers[sym] = name

        # Build sorted output list
        output = [{"symbol": s, "name": tickers[s]} for s in sorted(tickers.keys())]

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        self.stdout.write(self.style.SUCCESS(f"Saved {len(output)} tickers to {out_path}"))
        self.stdout.write(f"Updated at {datetime.utcnow().isoformat()}Z")

