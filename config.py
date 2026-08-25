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

# Stablecoin / fiat-proxy bases can never be a valid momentum setup, but they
# still passed every filter and burned the scarce AI budget. Filtered out at
# symbol-discovery time (scanner.get_active_usdt_symbols).
EXCLUDED_BASES = frozenset({
    "USD1", "USDC", "USDE", "USDP", "USDD", "USDT", "FDUSD", "TUSD", "BUSD",
    "DAI", "PYUSD", "RLUSD", "BFUSD", "AEUR", "EUR", "EURI", "GBP", "TRY",
    "BRZ", "XUSD", "USDS", "SUSD", "U",
})

# Concurrent OHLCV fetching. ccxt sync clients are not thread-safe, so each
# worker gets its own exchange instance; a shared token bucket keeps the total
# request rate under Binance's futures weight budget (2400/min; klines = 5).
FETCH_MAX_WORKERS = 8
FETCH_RATE_LIMIT_PER_SEC = 6.0  # 6 req/s * 5 weight * 60 = 1800 weight/min

# OHLCV cache: a closed candle never changes, so re-fetching 1H data for ~100
# coins every 5 minutes is pure waste. TTL per timeframe = one candle period.
OHLCV_CACHE_ENABLED = True
MARKETS_RELOAD_MIN = 60         # reload_markets at most once an hour, not per scan

# ------------------------------------------------------------------ indicators (pandas-ta)
RSI_LENGTH = 14
EMA_LENGTH = 21
BB_LENGTH = 20
BB_STD = 2
ATR_LENGTH = 14
RSI_HISTORY = 10                # store last 10 RSI values for trend analysis
VWAP_ANCHOR = "D"               # daily VWAP

# ------------------------------------------------------------------ 1H filter
# Handwritten strategy note (strategy_spec.md) is the source of truth:
#   BUY : RSI 50->70, EMA21 above, VWAP above, volume increase, Bollinger,
#         "Bottom to inbetween" + liquidation sweep
#   SELL: mirrored, "Top to inbetween" + liquidation sweep
#
# EMA21 and VWAP stay HARD gates because they define direction. Everything
# else is graded, so "bottom" scores full and "inbetween" scores partial
# instead of a pass/fail cliff.

# Zone: fraction of the 50-candle range measured from the favourable extreme.
# BUY uses range_pos, SELL uses (1 - range_pos).
ZONE_FULL_PCT = 0.30            # <=30% into the range = the note's "Bottom"/"Top" -> full points
ZONE_MAX_PCT = 0.60             # 30-60% = "inbetween" -> tapered points; >60% = wrong half, reject

# RSI: the note's band scores full; the wider band that production has been
# running scores half. Outside the wide band the coin is rejected outright.
RSI_BUY_FULL_MIN, RSI_BUY_FULL_MAX = 50.0, 70.0    # the handwritten note
RSI_BUY_TOL_MIN, RSI_BUY_TOL_MAX = 45.0, 80.0      # tolerance band -> reduced score
RSI_SELL_FULL_MIN, RSI_SELL_FULL_MAX = 35.0, 50.0  # the handwritten note
RSI_SELL_TOL_MIN, RSI_SELL_TOL_MAX = 22.0, 52.0    # tolerance band -> reduced score

RSI_OVERBOUGHT = 78.0           # 1H RSI above this -> reject BUY (reversal trap risk)
RSI_OVERSOLD = 25.0             # 1H RSI below this -> reject SELL (bounce risk)

# ------------------------------------------------------------------ confluence scoring
# Each timeframe scores 0-100. 1H carries zone + sweep; 15M/5M score only the
# three graded indicator conditions, reweighted so every timeframe stays on the
# same 0-100 scale and the weighted total below is meaningful.
W_1H_ZONE = 25
W_1H_RSI = 20
W_1H_VOLUME = 15
W_1H_BB = 15
W_1H_SWEEP = 25                 # heavy weight: the note lists sweep as a condition
W_LTF_RSI = 40
W_LTF_VOLUME = 30
W_LTF_BB = 30

ZONE_TAPER_FLOOR = 0.40         # score at ZONE_MAX_PCT as a fraction of W_1H_ZONE (25 -> 10)
RSI_TOL_FRACTION = 0.50         # tolerance-band RSI scores half
VOLUME_AVG_FRACTION = 0.67      # above 20-avg but not above previous candle
BB_MID_FRACTION = 0.47          # between the band and the mid-line

SWEEP_AGE_FULL = 2              # sweep within 2 candles -> full weight
SWEEP_AGE_PARTIAL = 5           # 3-5 candles -> 60%
SWEEP_AGE_STALE = 10            # 6-10 candles -> 32%; older counts as no sweep
SWEEP_PARTIAL_FRACTION = 0.60
SWEEP_STALE_FRACTION = 0.32

MIN_SCORE_1H = 55               # 1H confluence gate (a no-sweep setup can still reach this)
MIN_SCORE_15M = 50              # 15M confirmation gate
MIN_SCORE_5M = 50               # 5M entry gate
MIN_CONFLUENCE = 55             # weighted total gate

CONFLUENCE_W_1H = 0.40          # weights must sum to 1.0
CONFLUENCE_W_15M = 0.30
CONFLUENCE_W_5M = 0.30

# Sweep is required for a FULL-confidence alert: without it the confidence is
# capped below the HIGH band and the alert is labelled "no sweep".
NO_SWEEP_CONFIDENCE_CAP = 69.0

# ------------------------------------------------------------------ liquidation sweep
# Values below were tuned by hand on the live server (loosened from 5/2.0 after
# the strict pair produced whole sessions with zero sweeps). Carried over here.
SWEEP_SEARCH_CANDLES = 10       # how far back a sweep still counts
SWEEP_WINDOW = 20               # swing high/low lookback (last 20 candles)
SWEEP_WICK_BODY_RATIO = 2.0     # wick > 2x body
SWEEP_VOL_RATIO = 1.5           # liquidation volume vs 20-candle average

# ------------------------------------------------------------------ shared tolerances
BB_NEAR_PCT = 0.008             # 15M/5M: within 0.8% of the band (futures volatility)
BB_NEAR_PCT_1H = 0.035          # 1H: within 3.5% (server-tuned; 1H ranges are wider)
BB_BANDWIDTH_MIN = 0.015        # BB bandwidth below 1.5% = sideways/squeeze, skip coin

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
# Secondary free model, tried when the primary fails every retry. Only then does
# the run fall back to the pure-Python indicator decision.
AI_MODEL_FALLBACK = os.getenv("AI_MODEL_FALLBACK", "deepseek/deepseek-chat-v3.1:free")
AI_MAX_TOKENS = 2000            # was 300: truncated single answers mid-"reason"
                                # (finish_reason=length) and cannot hold a batch
AI_TIMEOUT_SECONDS = 60.0
AI_TEMPERATURE = 0.1
# Reasoning is DISABLED on purpose: the model burns the token budget on
# chain-of-thought and the final JSON never gets produced (finish_reason=length).
# Verified via scripts/openrouter_diagnose.py (Test C works, Test D starves).
AI_REASONING_ENABLED = False

# Batching: one request carries every candidate from a scan and returns a JSON
# array. This is what keeps the bot inside the free tier's 50 requests/day and
# collapses ~10s-per-candidate into a single round trip.
AI_BATCH_ENABLED = True
AI_BATCH_MAX = 12               # candidates per request; more than this is chunked
AI_RETRY_MAX = 3                # retry 429 / 5xx / timeout / malformed JSON
AI_RETRY_BACKOFF_BASE = 1.0     # 1s, 2s, 4s
AI_DAILY_BUDGET = int(os.getenv("AI_DAILY_BUDGET", "50"))  # OpenRouter free tier cap

# ------------------------------------------------------------------ duplicate guard
DUPLICATE_COOLDOWN_MIN = 15     # same coin within 15 min -> skip (futures pace faster)
GUARD_RESET_TIME = "23:00"      # tracker resets at 11:00 PM IST

# ------------------------------------------------------------------ scheduler
SESSION_START = "18:00"         # 6:00 PM IST
SESSION_END = "23:00"           # 11:00 PM IST
SCAN_INTERVAL_MIN = 5           # every 5 minutes
SCHEDULER_TZ = "Asia/Kolkata"
# Fire a few seconds AFTER the candle boundary: at :00 exactly the just-closed
# 5M candle may not be published yet, and fetch_ohlcv would silently hand back
# the previous candle as "entry".
SCAN_SECOND_OFFSET = 15
# APScheduler defaults (max_instances=1, misfire_grace_time=1s) silently DROP a
# scan whose predecessor is still running. Set explicitly instead.
SCAN_MISFIRE_GRACE_SEC = 120
# Hard deadline: past this the scan stops calling the AI, falls back to the
# Python decision for whatever is left, and warns. A scan can then never bleed
# into the next 5-minute slot.
SCAN_DEADLINE_SECONDS = 240

# ------------------------------------------------------------------ files
SIGNALS_LOG_FILE = BASE_DIR / "signals_log.csv"
BOT_LOG_FILE = BASE_DIR / "bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

CSV_COLUMNS = ["timestamp", "coin", "signal", "entry", "SL", "TP", "RR",
               "leverage", "position_size", "funding_rate", "confidence",
               "confluence", "score_1h", "score_15m", "score_5m",
               "sweep", "sweep_age", "rsi_bounce", "reason", "ai_used"]


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
