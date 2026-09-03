"""Observed market-data snapshots for news events — measurements, not claims.

Gathers price / 1h / 24h change / volume / OI / funding / liquidation context
for the assets a VERIFIED news event mentions, using the same public-exchange
transport the trading scanner uses. Everything returned is explicitly
labeled OBSERVED: the AI prompt receives it with instructions never to
attribute causality to the news.

Failures degrade: a failed lookup yields empty `observed` + an explicit
`unavailable` list, never fabricated numbers. Fields the exchange does not
expose stay None and render as "n/a" — a missing measurement is never
reported as a flat 0.0% change.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("news_market")

_ALIAS = {"BTC": "BTC/USDT:USDT", "ETH": "ETH/USDT:USDT"}


def _ccxt_symbol(asset: str) -> str:
    if "/" in asset:
        return asset
    base = asset.split(":")[0].upper()
    if base in _ALIAS:
        return _ALIAS[base]
    return f"{base}/USDT:USDT"


def _change_1h_pct(exchange, symbol: str) -> Optional[float]:
    """Real 1h change from the last closed 1H candle -> now.

    Binance's 24h ticker has no 1h field; defaulting it to 0.0 would fabricate
    a flat hour, so the value is measured (or None when the klines call
    fails). rows = [last_closed_1h, forming_1h]; the forming candle's close
    tracks the live price."""
    try:
        rows = exchange.fetch_ohlcv(symbol, "1h", limit=2)
        if rows and len(rows) >= 2 and rows[0][4] and rows[1][4]:
            return (rows[1][4] - rows[0][4]) / rows[0][4] * 100.0
    except Exception as exc:
        log.debug("1h change lookup failed for %s: %s", symbol, exc)
    return None


def observe(assets: list, exchange=None, max_assets: int = 4) -> dict:
    """Snapshot market context for the given assets.

    Returns {"observed": [rows], "unavailable": [symbols]}. Each observed row:
    symbol, price, change_1h_pct, change_24h_pct, volume_change_pct,
    open_interest, funding_rate, liquidation (from the websocket cache when
    running). Missing values are None — never invented.
    """
    if not assets:
        return {"observed": [], "unavailable": []}

    own_exchange = exchange is None
    if own_exchange:
        import scanner
        exchange = scanner.make_exchange()

    observed, unavailable = [], []
    for asset in [a for a in assets if a][:max_assets]:
        symbol = _ccxt_symbol(asset)
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = ticker.get("last")
            if price is None:
                unavailable.append(symbol)
                continue
            row = {
                "symbol": symbol,
                "price": price,
                "change_1h_pct": _change_1h_pct(exchange, symbol),
                "change_24h_pct": ticker.get("percentage"),  # None -> "n/a"
                "volume_change_pct": None,          # not exposed by fetch_ticker
                "open_interest": None,
                "funding_rate": None,
                "liquidation": None,
            }
            try:
                fr = exchange.fetch_funding_rate(symbol)
                row["funding_rate"] = fr.get("fundingRate")
            except Exception:
                pass
            try:
                oi = exchange.fetch_open_interest(symbol)
                if isinstance(oi, dict):
                    row["open_interest"] = oi.get("openInterestAmount")
            except Exception:
                pass
            try:
                import liquidation
                summary = liquidation.get_summary(asset)
                if summary.get("available"):
                    one_hour = summary["windows"]["1h"]
                    row["liquidation"] = (
                        f"1h liq: {one_hour['long_count']}L/{one_hour['short_count']}S")
            except Exception:
                pass
            observed.append(row)
        except Exception as exc:
            log.debug("market observe failed for %s: %s", symbol, exc)
            unavailable.append(symbol)

    if own_exchange:
        try:
            exchange.close()
        except Exception:
            pass
    return {"observed": observed, "unavailable": unavailable}
