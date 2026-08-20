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
VOLUME_MIN_USDT = 5_000_000     # 24h quote volume filter ($5M minimum)
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
ZONE_PCT = 0.30                 # bottom 30% / top 30% of 50-candle range
RSI_BUY_MIN, RSI_BUY_MAX = 40.0, 75.0    # bullish range (widened for early entries)
RSI_SELL_MIN, RSI_SELL_MAX = 28.0, 55.0  # bearish range (widened for strong trends)

# ------------------------------------------------------------------ liquidation sweep
SWEEP_SEARCH_CANDLES = 10       # look for the sweep candle among last N closed candles
SWEEP_WINDOW = 20               # swing high/low lookback (last 20 candles)
SWEEP_WICK_BODY_RATIO = 2.0     # wick > 2x body
SWEEP_VOL_RATIO = 1.5           # volume > 1.5x avg volume of last 20 candles

# ------------------------------------------------------------------ 15M filter
CONFIRM_MIN_SCORE = 4           # need 4/5 conditions
ENTRY_MIN_SCORE = 5             # 5M: need 5/7 conditions (scoring system)

# ------------------------------------------------------------------ shared tolerances
BB_NEAR_PCT = 0.005             # 15M/5M: "touch/near" = within 0.5% of the band
BB_NEAR_PCT_1H = 0.005          # 1H context uses a slightly wider near-band tolerance

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
DUPLICATE_COOLDOWN_MIN = 20     # same coin within 20 min -> skip silently
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

CSV_COLUMNS = ["timestamp", "coin", "signal", "entry", "SL", "TP", "RR", "reason", "ai_used"]


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
