"""Central configuration for the Crypto Signal Bot.

All tunables live here. Secrets are read from .env (never hardcoded).
Specs: rules.md / Architecture.md.
"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Kolkata")  # IST = UTC+5:30, no DST

# ------------------------------------------------------------------ secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# ------------------------------------------------------------------ scanner
EXCHANGE_ID = "binance"
VOLUME_MIN_USDT = 50_000_000    # 24h quote volume filter ($50M — futures liquidity)
CANDLE_LIMIT = 50               # last 50 candles per timeframe (strategy window)
INDICATOR_WARMUP = 250          # extra closed candles so pandas-ta values converge
                                # to TradingView (Wilder/EMA recursions need warm-up)
FETCH_RETRY_MAX = 3             # exchange fetch fails -> retry 3x -> skip coin
TIMEFRAMES = ("1h", "15m", "5m")

# ------------------------------------------------------------------ indicators (pandas-ta)
RSI_LENGTH = 14
EMA_LENGTH = 21
BB_LENGTH = 20
BB_STD = 2
ATR_LENGTH = 14
RSI_HISTORY = 10                # store last 10 RSI values for trend analysis
VWAP_ANCHOR = "D"               # daily VWAP

# ------------------------------------------------------------------ 1H filter
ZONE_PCT = 0.40                 # bottom 40% / top 40% of 50-candle range (futures momentum)
RSI_BUY_MIN, RSI_BUY_MAX = 45.0, 80.0    # futures: stronger momentum confirmation
RSI_SELL_MIN, RSI_SELL_MAX = 22.0, 52.0  # futures: catch aggressive liquidation selloffs
RSI_OVERBOUGHT = 78.0           # 1H RSI above this -> reject BUY (reversal trap risk)
RSI_OVERSOLD = 25.0             # 1H RSI below this -> reject SELL (bounce risk)

# ------------------------------------------------------------------ liquidation sweep
SWEEP_SEARCH_CANDLES = 5        # futures sweeps resolve fast, fresh data only
SWEEP_WINDOW = 20               # swing high/low lookback (last 20 candles)
SWEEP_WICK_BODY_RATIO = 2.0     # wick > 2x body
SWEEP_VOL_RATIO = 2.0           # futures: demand real liquidation volume (2x avg)

# ------------------------------------------------------------------ 15M filter
CONFIRM_MIN_SCORE = 4           # 15M: need 4/5 (core indicators mandatory)
ENTRY_MIN_SCORE = 5             # 5M: need 5/7 (core mandatory + at least 1 bonus)

# ------------------------------------------------------------------ shared tolerances
BB_NEAR_PCT = 0.008             # 15M/5M: within 0.8% of the band (futures volatility)
BB_NEAR_PCT_1H = 0.012          # 1H: within 1.2% (1H futures candles have bigger ranges)
BB_BANDWIDTH_MIN = 0.025        # BB bandwidth below 2.5% = sideways/squeeze, skip coin

# ------------------------------------------------------------------ funding rate (futures)
FUNDING_RATE_MAX_LONG = 0.0005   # +0.05% — reject BUY above this (longs overleveraged)
FUNDING_RATE_MIN_SHORT = -0.0003 # -0.03% — reject SELL below this (shorts overleveraged)

# ------------------------------------------------------------------ risk management
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "1000"))  # USDT balance for sizing
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "2.0"))  # max 2% risk per trade

# ------------------------------------------------------------------ leverage suggestion (ATR-based)
LEV_ATR_LOW = 0.01               # ATR < 1% of price -> high leverage OK
LEV_ATR_HIGH = 0.03              # ATR > 3% of price -> low leverage only

# ------------------------------------------------------------------ AI (OpenRouter / Nemotron)
AI_MODEL = os.getenv("AI_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
AI_MAX_TOKENS = 300             # rules.md: max tokens 300
AI_TIMEOUT_SECONDS = 60.0
AI_TEMPERATURE = 0.1
# Reasoning is DISABLED on purpose: with max_tokens capped at 300 the model
# burns the whole budget on chain-of-thought and the final JSON never gets
# produced (finish_reason=length). Verified via scripts/openrouter_diagnose.py
# (Test C works, Test D starves). Flip to True only with a larger token cap.
AI_REASONING_ENABLED = False

# ------------------------------------------------------------------ duplicate guard
DUPLICATE_COOLDOWN_MIN = 15     # same coin within 15 min -> skip (futures pace faster)
GUARD_RESET_TIME = "23:00"      # tracker resets at 11:00 PM IST

# ------------------------------------------------------------------ scheduler
SESSION_START = "18:00"         # 6:00 PM IST
SESSION_END = "23:00"           # 11:00 PM IST
SCAN_INTERVAL_MIN = 5           # every 5 minutes
SCHEDULER_TZ = "Asia/Kolkata"

# ------------------------------------------------------------------ files
SIGNALS_LOG_FILE = BASE_DIR / "signals_log.csv"
BOT_LOG_FILE = BASE_DIR / "bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

CSV_COLUMNS = ["timestamp", "coin", "signal", "entry", "SL", "TP", "RR",
               "leverage", "position_size", "funding_rate", "reason", "ai_used"]


def setup_logging() -> None:
    """Configure root logging: console + rotating file. Never print()."""
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            BOT_LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        ),
    ]
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    # quiet noisy third-party loggers
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
