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
# AI_BASE_URL keeps the transport configurable (any OpenAI-compatible provider)
# but defaults to OpenRouter.
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
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
GUARD_RESET_TIME = "00:00"      # tracker resets at midnight IST

# ------------------------------------------------------------------ scheduler
# Retained for compatibility with status/config consumers; scanning is 24/7.
SESSION_START = "00:00"
SESSION_END = "24:00"
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

# ==================================================================
# PRICE-ACTION & MARKET-CONTEXT-FIRST DECISION SYSTEM
# ==================================================================
# The block below powers the deterministic decision core (decision.py) and its
# detectors. Market structure / S-R / liquidity / price-action decide direction
# and quality; the indicators above are demoted to a bounded SECONDARY
# confirmation. Every threshold is a named constant here — the detectors carry
# no magic numbers. NO_TRADE is a valid, preferred output.

# ---- timeframe roles (reuse the existing 1H/15M/5M fetch; roles configurable)
TF_HTF = "1h"                    # higher-timeframe bias / market context
TF_SETUP = "15m"                 # setup / structure confirmation
TF_ENTRY = "5m"                  # entry trigger / execution timeframe

# ---- market structure (fractal swings, HH/HL/LH/LL, BOS, CHoCH, displacement)
STRUCT_LOOKBACK = 50             # recent candles analysed for structure
STRUCT_PIVOT_LEFT = 3            # fractal pivot: strictly-higher/lower bars to the left
STRUCT_PIVOT_RIGHT = 3           # ...and to the right (confirmation lag = RIGHT bars)
STRUCT_MIN_SWINGS = 4            # need this many swings to classify a trend
STRUCT_TREND_SWINGS = 4          # swings inspected for the HH/HL vs LH/LL verdict
STRUCT_ATR_LENGTH = 14           # ATR used for structure/zone tolerances (simple RMA)
STRUCT_DISPLACEMENT_ATR = 1.5    # body > this * ATR = displacement/impulse candle
STRUCT_RANGE_ATR = 1.0           # swing span < this * ATR over lookback = range/consolidation
STRUCT_RETEST_ATR = 0.5         # price within this * ATR of a broken level = retest

# ---- support / resistance (horizontal ZONES = ranges, never single prices)
SR_LOOKBACK = 50
SR_CLUSTER_ATR_MULT = 0.5        # levels within this * ATR merge into one zone
SR_ZONE_PAD_ATR = 0.25           # half-width padding of a zone built from one level
SR_MIN_TOUCHES = 2               # a zone needs at least this many touches to count
SR_MAJOR_TOUCHES = 4             # touches >= this -> major zone (else minor)
SR_PROXIMITY_ATR = 1.0           # price within this * ATR of a zone = "at" the zone
SR_WICK_BONUS = 0.5              # rejection-wick touches score this extra vs body touches
SR_MAX_ZONES = 8                 # keep the strongest N zones per side (noise guard)

# ---- liquidity & sweeps (equal highs/lows = stop pools; reclaim + confirm)
LIQ_EQUAL_TOL_ATR = 0.15         # highs/lows within this * ATR are "equal" (a pool)
LIQ_MIN_EQUAL = 2                # this many equal extremes = a liquidity pool
LIQ_RECLAIM_CANDLES = 3          # reclaim of the swept level must occur within N candles
LIQ_CONFIRM_REQUIRED = True      # MANDATORY post-sweep confirmation candle (no touch-only entries)
LIQ_CONFIRM_CLOSE_ATR = 0.0      # confirmation close must clear the level by this * ATR

# ---- price action & volume
PA_REJECTION_WICK_RATIO = 2.0    # dominant wick >= this * body = rejection candle
PA_ENGULF_MIN_RATIO = 1.0        # engulfing body must exceed the prior body by this factor
PA_DISPLACEMENT_ATR = 1.5        # body > this * ATR = displacement
PA_VOLUME_STRONG = 1.5           # volume >= this * avg20 = strong/confirmed
PA_VOLUME_WEAK = 0.8             # volume <  this * avg20 = weak (no-volume breakout flag)
PA_BREAKOUT_LOOKBACK = 20        # window whose high/low defines a breakout level

# ---- trendlines / channels (confluence only, never standalone)
TL_LOOKBACK = 50
TL_MIN_TOUCHES = 3               # a valid trendline needs at least this many swing touches
TL_TOLERANCE_ATR = 0.3           # a swing within this * ATR of the line counts as a touch
TL_MAX_SLOPE_PCT = 0.05          # per-candle slope beyond this % of price = too steep, discard
TL_BREAK_ATR = 0.25              # close beyond the line by this * ATR = break

# ---- multi-timeframe alignment (MANDATORY)
MTF_REQUIRE_HTF_ALIGN = True     # entry-TF bias opposing HTF bias -> reject
MTF_NEUTRAL_HTF_PENALTY = 10     # HTF neutral (range) -> shave this many quality points
MTF_COUNTER_SETUP_PENALTY = 15   # setup-TF disagrees with HTF -> shave this many points

# ---- crypto-futures context (OI + funding now; basis / L-S ratio flagged for later)
OI_FETCH_ENABLED = True          # fetch open-interest history per candidate
OI_HISTORY_TIMEFRAME = "5m"      # OI-history granularity
OI_HISTORY_LIMIT = 24            # this many OI points -> OI-change-vs-price read
OI_CHANGE_MIN_PCT = 0.01         # |OI change| below this % is treated as "flat"
FUNDING_EXTREME_LONG = 0.0010    # funding above this = crowded longs (context, not a gate)
FUNDING_EXTREME_SHORT = -0.0010  # funding below this = crowded shorts
BASIS_ENABLED = False            # config-flagged slot: spot-vs-futures basis (not built yet)
LONG_SHORT_RATIO_ENABLED = False # config-flagged slot: account long/short ratio (not built yet)

# ---- setup quality (primary evidence drives it; indicators are bounded/secondary)
QUALITY_W_STRUCTURE = 25         # primary-evidence weights (sum below = 100)
QUALITY_W_SR = 20
QUALITY_W_LIQUIDITY = 20
QUALITY_W_PRICE_ACTION = 15
QUALITY_W_MTF = 10
QUALITY_W_TRENDLINE = 5
QUALITY_W_FUTURES = 5
QUALITY_PRIMARY_FLOOR = 45       # primary score below this -> NO_TRADE (indicators cannot rescue)
IND_CONFIRM_BONUS_MAX = 10       # aligned indicators add at most this (cannot trigger alone)
IND_CONFLICT_PENALTY_MAX = 15    # opposing indicators shave at most this (secondary yields)
QUALITY_MIN = 55                 # final setup-quality gate for a tradable setup

# ---- setup-quality exhaustion & location penalties
# A score that only asks "how strongly does each layer agree with the
# direction?" ranks an exhausted SELL sitting on the 1H low (RSI oversold,
# below the lower band, no volume, no room to fall) as highly as a fresh
# short from the range top. The terms below push price LOCATION, RSI
# exhaustion, Bollinger stretch, volume and achievable RR into the score as
# multiplicative factors / subtractive points, so quality reflects tradable
# setups rather than how bearish/bullish the tape looks.
QUALITY_LOCATION_SEVERE_PCT = 0.15    # within 15% of the WRONG 1H range extreme
QUALITY_LOCATION_MODERATE_PCT = 0.30  # 15-30% from the wrong extreme
QUALITY_LOCATION_MILD_PCT = 0.45      # 30-45% from the wrong extreme
QUALITY_LOCATION_SEVERE_FACTOR = 0.55
QUALITY_LOCATION_MODERATE_FACTOR = 0.75
QUALITY_LOCATION_MILD_FACTOR = 0.90
QUALITY_SWEEP_EXEMPT_FACTOR = 0.50    # a CONFIRMED sweep halves the location penalty
QUALITY_RSI_OVERSOLD = 30.0           # SHORT below this = exhausted, not fresh
QUALITY_RSI_OVERBOUGHT = 70.0         # LONG above this = exhausted, not fresh
QUALITY_RSI_EXHAUSTION_FACTOR = 0.70
QUALITY_BB_EXTREME_FACTOR = 0.85      # beyond the band in the trade's direction
QUALITY_WEAK_VOLUME_FACTOR = 0.90     # last 1H volume < PA_VOLUME_WEAK x avg20
QUALITY_DECLINING_VOLUME_FACTOR = 0.95  # volume merely below the prior candle
QUALITY_RR_NONE_PENALTY = 30.0        # no achievable opposing target at all
QUALITY_RR_MISS_PENALTY = 20.0        # rr < MIN_RR, scaled by how far it misses
MTF_TREND_CONFLICT_PENALTY = 10       # fresh CHoCH against the standing HTF trend

# ---- directional-confirmation gate (a structural bias alone is not a trade)
# direction_gate.confirm() cross-checks the structure-proposed direction
# against EMA21/VWAP positioning on the tradeable TFs (15M+5M), entry RSI
# momentum, and genuine confirming events (BOS / displacement / confirmed
# sweep / CHoCH+retest) BEFORE the setup is scored or ranked.
DIR_CONFLICT_FACTOR = 0.30             # positioning+momentum contradict, nothing confirms
DIR_CONFLICT_LATE_FACTOR = 0.60        # same, but structure freshly confirmed (fought entry)
DIR_POSITIONING_CONFLICT_FACTOR = 0.70 # price on the wrong EMA/VWAP side of tradeable TFs
DIR_MOMENTUM_CONFLICT_FACTOR = 0.80    # entry momentum against, no confirming event
DIR_MOMENTUM_CONFLICT_CONFIRMED_FACTOR = 0.90  # momentum against but structure confirmed
DIR_UNCONFIRMED_CHOCH_FACTOR = 0.75    # CHoCH-driven direction lacking post-CHoCH confirmation

# ---- risk / reward gate (MANDATORY; never bypasses existing sizing limits)
MIN_RR = 1.5                     # configurable minimum reward:risk
RISK_SL_BUFFER_ATR = 0.5         # SL placed beyond the structural invalidation by this * ATR
RISK_MAX_STOP_ATR = 3.0          # stop wider than this * ATR -> NO_TRADE (poor structure)
RISK_MIN_TARGET_ATR = 1.0        # nearest opposing zone closer than this * ATR -> NO_TRADE
RISK_MAX_SPREAD_PCT = 0.0015     # spread wider than this (when known) -> NO_TRADE
RISK_TARGET_ZONE_PAD_ATR = 0.25  # target placed this * ATR short of the opposing zone edge

# ---- decision engine
DECISION_ENABLED = True          # master switch: price-action core decides (vs legacy funnel)

# ---- news verification engine (VERIFY FIRST — AI never decides what is true)
NEWS_ENABLED = True              # start the persistent news engine with the bot
NEWS_POLL_SECONDS = 300          # RSS polling interval
NEWS_EVENT_WINDOW_HOURS = 24     # articles older than this cannot join/confirm an event
NEWS_MAX_ARTICLES_PER_CYCLE = 30
NEWS_SIMILARITY_MIN = 0.35       # token-Jaccard threshold for "same event" clustering
NEWS_MATERIAL_TOKEN_FRAC = 0.30  # new claim tokens above this fraction = material update
NEWS_AI_DAILY_LIMIT = 100        # separate from the trading-prompt budget
NEWS_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
)
# Domains that count as PRIMARY/official sources (exchange, regulator, project).
# An event is VERIFIED only with one of these PLUS an independent confirmation.
NEWS_OFFICIAL_DOMAINS = frozenset({
    "binance.com", "coinbase.com", "kraken.com", "okx.com", "bybit.com",
    "sec.gov", "treasury.gov", "federalreserve.gov", "ecb.europa.eu",
    "ethereum.org", "bitcoin.org", "solana.com", "ripple.com",
})

# ------------------------------------------------------------------ files
LIQUIDATION_RECONNECT_SECONDS = 5
LIQUIDATION_BURST_COUNT = 3
SIGNALS_LOG_FILE = BASE_DIR / "signals_log.csv"
BOT_LOG_FILE = BASE_DIR / "bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

CSV_COLUMNS = ["timestamp", "coin", "signal", "entry", "SL", "TP", "RR",
               "leverage", "position_size", "funding_rate", "confidence",
               "confluence", "score_1h", "score_15m", "score_5m",
                "sweep", "sweep_age", "rsi_bounce", "reason", "ai_used",
                "liquidation",
               # structure-first decision core (Phase 6): the primary evidence
               "decision", "setup_quality", "htf_bias", "structure",
               "sr_zone", "liquidity", "no_trade_reason", "data_warnings"]


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
