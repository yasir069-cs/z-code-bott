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
# OpenRouter is the default provider. Legacy NVIDIA/AgentRouter key names remain
# accepted so existing deployments keep working when their endpoint is explicit.
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY")
                      or os.getenv("AGENTROUTER_API_KEY")
                      or NVIDIA_API_KEY).strip()

# ------------------------------------------------------------------ scanner
EXCHANGE_ID = "binance"
VOLUME_MIN_USDT = 50_000_000    # 24h quote volume filter ($50M — futures liquidity)
CANDLE_LIMIT = 50               # last 50 candles per timeframe (strategy window)
INDICATOR_WARMUP = 250          # extra closed candles so pandas-ta values converge
                                # to TradingView (Wilder/EMA recursions need warm-up)
FRAME_MIN_CANDLES = 40
FETCH_RETRY_MAX = 3             # exchange fetch fails -> retry 3x -> skip coin
FETCH_TIMEOUT_MS = 15000        # explicit per-request ccxt timeout: no request
                                # may hang indefinitely, whatever the deadline
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
ZONE_FULL_PCT = 0.30            # <=30% into the range = the note's "Bottom"/"Top" -> full points
ZONE_MAX_PCT = 0.60             # 30-60% = "inbetween" -> tapered points; >60% = wrong half, reject

RSI_BUY_FULL_MIN, RSI_BUY_FULL_MAX = 50.0, 70.0    # the handwritten note
RSI_BUY_TOL_MIN, RSI_BUY_TOL_MAX = 45.0, 80.0      # tolerance band -> reduced score
RSI_SELL_FULL_MIN, RSI_SELL_FULL_MAX = 35.0, 50.0  # the handwritten note
RSI_SELL_TOL_MIN, RSI_SELL_TOL_MAX = 22.0, 52.0    # tolerance band -> reduced score

RSI_OVERBOUGHT = 78.0           # 1H RSI above this -> reject BUY (reversal trap risk)
RSI_OVERSOLD = 25.0             # 1H RSI below this -> reject SELL (bounce risk)

# ------------------------------------------------------------------ confluence scoring
W_1H_ZONE = 25
W_1H_RSI = 20
W_1H_VOLUME = 15
W_1H_BB = 15
W_1H_SWEEP = 25                 # heavy weight: the note lists sweep as a condition
W_LTF_RSI = 40
W_LTF_VOLUME = 30
W_LTF_BB = 30

# ------------------------------------------------------------------ tuned floors
def _env_number(name: str, default: float) -> float:
    """A tuned number: the `.env` value when present and numeric, else the default.

    A non-numeric override raises at import instead of being ignored — silently
    falling back to the default is how "we loosened it and nothing changed" gets
    reported for an override that never took effect.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name}={raw!r} in .env is not a number") from None


def _env_flag(name: str, default: bool) -> bool:
    """A boolean switch from `.env` (`0/false/no/off` vs anything else)."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "0", "true", "false", "yes", "no", "on", "off"):
        return raw not in ("0", "false", "no", "off")
    raise RuntimeError(f"{name}={raw!r} in .env is not a flag (use 1/0, true/false, on/off)")


ZONE_TAPER_FLOOR = 0.40         # score at ZONE_MAX_PCT as a fraction of W_1H_ZONE (25 -> 10)
RSI_TOL_FRACTION = 0.50         # tolerance-band RSI scores half
VOLUME_AVG_FRACTION = 0.67      # above 20-avg but not above previous candle
BB_MID_FRACTION = 0.47          # between the band and the mid-line

SWEEP_AGE_FULL = 2              # sweep within 2 candles -> full weight
SWEEP_AGE_PARTIAL = 5           # 3-5 candles -> 60%
SWEEP_AGE_STALE = 10            # 6-10 candles -> 32%; older counts as no sweep
SWEEP_PARTIAL_FRACTION = 0.60
SWEEP_STALE_FRACTION = 0.32

MIN_SCORE_1H = _env_number("MIN_SCORE_1H", 55.0)      # 1H confluence gate
MIN_SCORE_15M = _env_number("MIN_SCORE_15M", 50.0)   # 15M confirmation gate
MIN_SCORE_5M = _env_number("MIN_SCORE_5M", 50.0)     # 5M entry gate
MIN_CONFLUENCE = _env_number("MIN_CONFLUENCE", 55.0)  # weighted total gate

CONFLUENCE_W_1H = 0.40          # weights must sum to 1.0
CONFLUENCE_W_15M = 0.30
CONFLUENCE_W_5M = 0.30

NO_SWEEP_CONFIDENCE_CAP = 59.0

# ------------------------------------------------------------------ liquidation sweep
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

# ==================================================================
# AI (OpenAI-compatible) — MULTI-PROVIDER FALLBACK POOL
# ==================================================================
# Legacy single-provider settings. Kept because:
#   (a) they still work standalone (one provider, no .env changes needed), and
#   (b) provider "0" in the pool below is built FROM these values, so an
#       existing deployment with just AI_BASE_URL/OPENROUTER_API_KEY/AI_MODEL
#       in .env keeps behaving exactly as before.
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free").strip()
AI_MODEL_FALLBACK = os.getenv("AI_MODEL_FALLBACK", "").strip()

AI_MAX_TOKENS = int(_env_number("AI_MAX_TOKENS", 2000))
# Per-request timeout. 90s suits cloud providers; a self-hosted Ollama model
# (e.g. qwen3:8b on CPU) needs more, so it is .env-overridable.
AI_TIMEOUT_SECONDS = _env_number("AI_TIMEOUT_SECONDS", 90.0)
AI_CHAT_DEADLINE_SECONDS = _env_number("AI_CHAT_DEADLINE_SECONDS", 45.0)
AI_TEMPERATURE = 0.1
AI_REASONING_ENABLED = False    # reasoning burns the token budget on
                                # chain-of-thought and the JSON never arrives
# OpenAI-style reasoning_effort value forwarded verbatim in the request payload
# ("" = do not send the field at all, keeping cloud providers untouched).
# Ollama maps "none" to think:false — this is what makes qwen3:8b answer a
# JSON verdict in ~25s instead of thinking for 10+ minutes.
AI_REASONING_EFFORT = os.getenv("AI_REASONING_EFFORT", "").strip().lower()

AI_BATCH_ENABLED = True
AI_BATCH_MAX = int(_env_number("AI_BATCH_MAX", 8))
AI_JSON_MODE = _env_flag("AI_JSON_MODE", True)
AI_RETRY_MAX = int(_env_number("AI_RETRY_MAX", 2))     # attempts PER PROVIDER before
                                # the ladder moves to the next one
AI_MAX_TOKENS_RETRY_CAP = int(os.getenv("AI_MAX_TOKENS_RETRY_CAP", "4000"))
AI_RETRY_BACKOFF_BASE = 1.0     # 1s, 2s, 4s

# Per-provider advisory daily cap. Each entry in AI_PROVIDERS gets its OWN
# independent counter of this size — this is NOT a global total. Six
# providers at 45/day = up to 270 requests/day across the whole pool, but any
# single provider stops itself at 45 regardless of the others' state.
AI_DAILY_BUDGET = int(os.getenv("AI_DAILY_BUDGET", "45"))


def _build_provider_pool() -> list[dict]:
    """The ordered fallback ladder: [{"name","base_url","api_key","model"}, ...].

    Provider 0 ("primary") is built from the legacy AI_BASE_URL/OPENROUTER_API_KEY/
    AI_MODEL trio so a single-provider .env keeps working unchanged. If
    AI_MODEL_FALLBACK names a second model, it is folded in right after provider 0
    as "primary_fallback_model" on the SAME base_url/key (a model-only fallback,
    not a new account).

    Beyond that, numbered providers are read from .env as a block of four vars:
        AI_PROVIDER_<N>_NAME      (optional, defaults to "provider_<N>")
        AI_PROVIDER_<N>_BASE_URL
        AI_PROVIDER_<N>_API_KEY
        AI_PROVIDER_<N>_MODEL
    starting at N=1 and stopping at the first N missing BASE_URL/API_KEY/MODEL.
    Adding a 6th OpenRouter account is therefore a four-line .env addition —
    never a code change. ai_decision._complete() tries them in this exact order,
    each with its own AI_RETRY_MAX attempts and its own AI_DAILY_BUDGET counter;
    a provider that is exhausted or failing is skipped, not retried forever.
    """
    providers: list[dict] = []
    seen_names: set[str] = set()

    def _add(name: str, base_url: str, api_key: str, model: str) -> None:
        if name in seen_names:
            name = f"{name}_{len(providers)}"
        seen_names.add(name)
        providers.append({"name": name, "base_url": base_url.rstrip("/"),
                          "api_key": api_key, "model": model})

    if OPENROUTER_API_KEY and AI_MODEL:
        _add("primary", AI_BASE_URL, OPENROUTER_API_KEY, AI_MODEL)
        if AI_MODEL_FALLBACK:
            first_fallback = next(
                (m.strip() for m in AI_MODEL_FALLBACK.split(",") if m.strip() and m.strip() != AI_MODEL),
                None)
            if first_fallback:
                _add("primary_fallback_model", AI_BASE_URL, OPENROUTER_API_KEY, first_fallback)

    i = 1
    while True:
        base = os.getenv(f"AI_PROVIDER_{i}_BASE_URL", "").strip()
        key = os.getenv(f"AI_PROVIDER_{i}_API_KEY", "").strip()
        model = os.getenv(f"AI_PROVIDER_{i}_MODEL", "").strip()
        if not (base and key and model):
            break
        name = os.getenv(f"AI_PROVIDER_{i}_NAME", f"provider_{i}").strip() or f"provider_{i}"
        _add(name, base, key, model)
        i += 1

    return providers


AI_PROVIDERS = _build_provider_pool()

# ------------------------------------------------------------------ duplicate guard
DUPLICATE_COOLDOWN_MIN = 15     # same coin within 15 min -> skip (futures pace faster)
GUARD_RESET_TIME = "23:00"      # tracker resets at 11:00 PM IST
HOLD_LOG_COOLDOWN_MIN = 30

# ------------------------------------------------------------------ scheduler
SESSION_START = "18:00"         # 6:00 PM IST
SESSION_END = "23:00"           # 11:00 PM IST
SCAN_INTERVAL_MIN = 5           # every 5 minutes
SCHEDULER_TZ = "Asia/Kolkata"
SCAN_SECOND_OFFSET = 15
SCAN_MISFIRE_GRACE_SEC = 120
# Bounded wall-clock budget for one scan. 210s keeps a scan inside its 5-minute
# slot on a cloud AI provider; a self-hosted local model (Ollama on CPU) can
# need far longer per request, so the budget is .env-overridable without a code
# edit. Overlapping slots are refused by the scan coordinator either way.
SCAN_DEADLINE_SECONDS = _env_number("SCAN_DEADLINE_SECONDS", 210.0)

# ---- hardening: bounded services, watchdogs, isolation
LIQ_STALE_SECONDS = 1800
NEWS_429_COOLDOWN_SECONDS = 600
NEWS_TIMEOUT_COOLDOWN_SECONDS = 300
AI_WORKER_MAX_PENDING = 2

# ==================================================================
# PRICE-ACTION & MARKET-CONTEXT-FIRST DECISION SYSTEM
# ==================================================================
TF_HTF = "1h"
TF_SETUP = "15m"
TF_ENTRY = "5m"

STRUCT_LOOKBACK = 50
STRUCT_PIVOT_LEFT = 3
STRUCT_PIVOT_RIGHT = 3
STRUCT_MIN_SWINGS = 4
STRUCT_TREND_SWINGS = 4
STRUCT_ATR_LENGTH = 14
STRUCT_DISPLACEMENT_ATR = 1.5
STRUCT_RANGE_ATR = 1.0
STRUCT_RETEST_ATR = 0.5

SR_LOOKBACK = 50
SR_CLUSTER_ATR_MULT = 0.5
SR_ZONE_PAD_ATR = 0.25
SR_MIN_TOUCHES = 2
SR_MAJOR_TOUCHES = 4
SR_PROXIMITY_ATR = 1.0
SR_WICK_BONUS = 0.5
SR_MAX_ZONES = 8

LIQ_EQUAL_TOL_ATR = 0.15
LIQ_MIN_EQUAL = 2
LIQ_RECLAIM_CANDLES = 3
LIQ_CONFIRM_REQUIRED = True
LIQ_CONFIRM_CLOSE_ATR = 0.0

PA_REJECTION_WICK_RATIO = 2.0
PA_ENGULF_MIN_RATIO = 1.0
PA_DISPLACEMENT_ATR = 1.5
PA_VOLUME_STRONG = 1.5
PA_VOLUME_WEAK = 0.8
PA_BREAKOUT_LOOKBACK = 20

TL_LOOKBACK = 50
TL_MIN_TOUCHES = 3
TL_TOLERANCE_ATR = 0.3
TL_MAX_SLOPE_PCT = 0.05
TL_BREAK_ATR = 0.25

MTF_REQUIRE_HTF_ALIGN = True
MTF_NEUTRAL_HTF_PENALTY = 10
MTF_COUNTER_SETUP_PENALTY = 15

OI_FETCH_ENABLED = True
OI_HISTORY_TIMEFRAME = "5m"
OI_HISTORY_LIMIT = 24
OI_CHANGE_MIN_PCT = 0.01
FUNDING_EXTREME_LONG = 0.0010
FUNDING_EXTREME_SHORT = -0.0010
BASIS_ENABLED = False
LONG_SHORT_RATIO_ENABLED = False

QUALITY_W_STRUCTURE = 25
QUALITY_W_SR = 20
QUALITY_W_LIQUIDITY = 20
QUALITY_W_PRICE_ACTION = 15
QUALITY_W_MTF = 10
QUALITY_W_TRENDLINE = 5
QUALITY_W_FUTURES = 5
QUALITY_PRIMARY_FLOOR = _env_number("QUALITY_PRIMARY_FLOOR", 45.0)
IND_CONFIRM_BONUS_MAX = _env_number("IND_CONFIRM_BONUS_MAX", 10.0)
IND_CONFLICT_PENALTY_MAX = _env_number("IND_CONFLICT_PENALTY_MAX", 15.0)
QUALITY_MIN = _env_number("QUALITY_MIN", 50.0)

QUALITY_LOCATION_SEVERE_PCT = _env_number("QUALITY_LOCATION_SEVERE_PCT", 0.15)
QUALITY_LOCATION_MODERATE_PCT = _env_number("QUALITY_LOCATION_MODERATE_PCT", 0.30)
QUALITY_LOCATION_MILD_PCT = _env_number("QUALITY_LOCATION_MILD_PCT", 0.45)
QUALITY_LOCATION_SEVERE_FACTOR = _env_number("QUALITY_LOCATION_SEVERE_FACTOR", 0.55)
QUALITY_LOCATION_MODERATE_FACTOR = _env_number("QUALITY_LOCATION_MODERATE_FACTOR", 0.75)
QUALITY_LOCATION_MILD_FACTOR = _env_number("QUALITY_LOCATION_MILD_FACTOR", 0.90)
QUALITY_SWEEP_EXEMPT_FACTOR = 0.50
QUALITY_RSI_OVERSOLD = 30.0
QUALITY_RSI_OVERBOUGHT = 70.0
QUALITY_RSI_EXHAUSTION_FACTOR = 0.70
QUALITY_BB_EXTREME_FACTOR = 0.85
QUALITY_WEAK_VOLUME_FACTOR = _env_number("QUALITY_WEAK_VOLUME_FACTOR", 0.90)
QUALITY_DECLINING_VOLUME_FACTOR = _env_number("QUALITY_DECLINING_VOLUME_FACTOR", 0.95)
QUALITY_RR_NONE_PENALTY = _env_number("QUALITY_RR_NONE_PENALTY", 30.0)
QUALITY_RR_MISS_PENALTY = _env_number("QUALITY_RR_MISS_PENALTY", 20.0)
MTF_TREND_CONFLICT_PENALTY = 10

DIR_CONFLICT_FACTOR = 0.30
DIR_CONFLICT_LATE_FACTOR = 0.60
DIR_POSITIONING_CONFLICT_FACTOR = 0.70
DIR_MOMENTUM_CONFLICT_FACTOR = 0.80
DIR_MOMENTUM_CONFLICT_CONFIRMED_FACTOR = 0.90
DIR_UNCONFIRMED_CHOCH_FACTOR = 0.75

MIN_RR = _env_number("MIN_RR", 1.5)
RISK_SL_BUFFER_ATR = 0.5
RISK_MAX_STOP_ATR = _env_number("RISK_MAX_STOP_ATR", 3.0)
RISK_MIN_TARGET_ATR = _env_number("RISK_MIN_TARGET_ATR", 1.0)
RISK_MAX_SPREAD_PCT = 0.0015
RISK_TARGET_ZONE_PAD_ATR = 0.25
RISK_TARGET_SCAN_ZONES = _env_flag("RISK_TARGET_SCAN_ZONES", True)

DECISION_ENABLED = True
LLM_DECISION_ENABLED = True      # False -> no AI call at all; Python decides everything

# ---- LLM eligibility gate (budget protection) ----
# Sending EVERY decided candidate (including setup_quality=0 rejects) to the AI
# emptied a whole day's free-tier budget in one scan (live 2026-09-04: 47/47
# sent, budget hit its cap before the scan even finished). Only candidates that
# already look tradeable on the deterministic score are worth an AI request; a
# quality=0 setup was going to be a Python HOLD regardless of the model's
# opinion. MAX_CANDIDATES_FOR_AI additionally caps the batch size per scan so
# one unusually strong scan cannot alone exhaust several days of every
# provider's budget.
MIN_QUALITY_FOR_AI = _env_number("MIN_QUALITY_FOR_AI", 35.0)
MAX_CANDIDATES_FOR_AI = int(_env_number("MAX_CANDIDATES_FOR_AI", 8))

# ---- alert tier system (owner's rule, 2026-09-01) ----
ALERT_QUALITY_MIN = _env_number("ALERT_QUALITY_MIN", 50.0)
ALERT_TIER_NORMAL_MIN = _env_number("ALERT_TIER_NORMAL_MIN", 50.0)
ALERT_TIER_HIGH_MIN = _env_number("ALERT_TIER_HIGH_MIN", 60.0)
ALERT_TIER_STRONG_MIN = _env_number("ALERT_TIER_STRONG_MIN", 70.0)

# ---- news verification engine ----
NEWS_ENABLED = False
NEWS_POLL_SECONDS = 300
NEWS_ALERT_COOLDOWN_SECONDS = 1200
NEWS_EVENT_WINDOW_HOURS = 24
NEWS_MAX_ARTICLES_PER_CYCLE = 30
NEWS_SIMILARITY_MIN = 0.35
NEWS_MATERIAL_TOKEN_FRAC = 0.30
NEWS_ALLOW_UPDATE_ALERTS = False
NEWS_ALERT_MEMORY_DAYS = 7
NEWS_ALERT_MEMORY_FILE = BASE_DIR / "news_alerted.json"
NEWS_AI_DAILY_LIMIT = 100
NEWS_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cointelegraph.com/rss/tag/donald-trump",
    "https://cointelegraph.com/rss/tag/politics",
    "https://blog.ethereum.org/feed.xml",
    "https://solana.com/news/rss.xml",
    "https://www.reddit.com/r/CryptoCurrency/.rss",
    "https://www.reddit.com/r/Bitcoin/.rss",
    "https://www.reddit.com/r/ethereum/.rss",
)
NEWS_OFFICIAL_DOMAINS = frozenset({
    "binance.com", "coinbase.com", "kraken.com", "okx.com", "bybit.com",
    "sec.gov", "treasury.gov", "federalreserve.gov", "ecb.europa.eu",
    "ethereum.org", "bitcoin.org", "solana.com", "ripple.com",
})
NEWS_SOCIAL_DOMAINS = frozenset({
    "reddit.com", "old.reddit.com", "np.reddit.com",
    "twitter.com", "x.com", "nitter.net",
})

# ------------------------------------------------------------------ files
LIQUIDATION_RECONNECT_SECONDS = 5
LIQUIDATION_BURST_COUNT = 3
LIQUIDATION_WINDOWS = {"5m": 300, "15m": 900, "1h": 3600}
SIGNALS_LOG_FILE = BASE_DIR / "signals_log.csv"
BOT_LOG_FILE = BASE_DIR / "bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

_EXPOSED_TELEGRAM_TOKENS = (
    "8851597372:AAFlynes",
)
_EXPOSED_OPENROUTER_KEYS = (
    "sk-or-v1-5e4bb826af4b",
    "sk-or-v1-87531e9388a2",
)


def check_exposed_credentials() -> list[str]:
    """Return warnings for any still-configured credential that is known to
    have been leaked publicly. Empty list = clean."""
    warnings = []
    if any(TELEGRAM_TOKEN.startswith(p) for p in _EXPOSED_TELEGRAM_TOKENS):
        warnings.append("TELEGRAM_TOKEN was exposed in chat — revoke it via "
                        "@BotFather /revoke and put the new token in .env")
    if any(OPENROUTER_API_KEY.startswith(p) for p in _EXPOSED_OPENROUTER_KEYS):
        warnings.append("OPENROUTER_API_KEY was exposed in chat — revoke it at "
                        "the provider's key page and put the new key in .env")
    for p in AI_PROVIDERS:
        if any(p["api_key"].startswith(pre) for pre in _EXPOSED_OPENROUTER_KEYS):
            warnings.append(f"AI provider '{p['name']}' uses a key that was exposed "
                            "in chat — revoke and replace it in .env")
    return warnings


def check_config_warnings() -> list[str]:
    """Non-secret .env mistakes that otherwise show up as 'the AI is silent'."""
    warnings = []
    from urllib.parse import urlparse

    if not AI_PROVIDERS:
        warnings.append("AI_PROVIDERS is empty — no usable AI_BASE_URL/OPENROUTER_API_KEY/"
                        "AI_MODEL and no AI_PROVIDER_1_* block found. Every decision falls "
                        "back to the Python core (this is safe, but LLM_DECISION_ENABLED "
                        "has nothing to call).")

    for p in AI_PROVIDERS:
        parsed = urlparse(p["base_url"])
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            warnings.append(f"provider '{p['name']}' base_url is not a usable URL "
                            f"({p['base_url']!r}) — every call to it will fail. Check for "
                            "quotes or a pasted markdown link; it must be a bare URL")
        elif any(c in p["base_url"] for c in "[]() "):
            warnings.append(f"provider '{p['name']}' base_url contains brackets/spaces "
                            f"({p['base_url']!r}) — looks like a copied markdown link")
        if not p["model"]:
            warnings.append(f"provider '{p['name']}' has an empty model name")
        if not p["api_key"]:
            warnings.append(f"provider '{p['name']}' has an empty api_key")

    names = [p["name"] for p in AI_PROVIDERS]
    if len(names) != len(set(names)):
        warnings.append("two AI providers ended up with the same name after de-duplication "
                        "— their budgets may be tracked as one; check AI_PROVIDER_*_NAME")

    for name, value, low, high in (
            ("QUALITY_PRIMARY_FLOOR", QUALITY_PRIMARY_FLOOR, 36.0, 90.0),
            ("QUALITY_MIN", QUALITY_MIN, 40.0, 90.0),
            ("ALERT_QUALITY_MIN", ALERT_QUALITY_MIN, 40.0, 95.0),
            ("MIN_RR", MIN_RR, 1.0, 5.0),
            ("RISK_MIN_TARGET_ATR", RISK_MIN_TARGET_ATR, 0.5, 5.0),
            ("RISK_MAX_STOP_ATR", RISK_MAX_STOP_ATR, 1.0, 10.0)):
        if not low <= value <= high:
            warnings.append(f"{name}={value:g} is outside the band [{low:g}, {high:g}] the "
                            "strategy is written against; the bot still runs, but its "
                            "acceptance rate no longer means what the docs claim")
    if QUALITY_RR_NONE_PENALTY <= 0 and QUALITY_RR_MISS_PENALTY <= 0 and MIN_RR <= 0:
        warnings.append("reward:risk is enforced NOWHERE: both quality RR penalties are 0 "
                        "and MIN_RR is off — every unmeasurable target becomes a signal")
    if ALERT_QUALITY_MIN > QUALITY_MIN:
        warnings.append(f"ALERT_QUALITY_MIN ({ALERT_QUALITY_MIN:g}) is above QUALITY_MIN "
                        f"({QUALITY_MIN:g}): setups clear the decision gate and are then "
                        "dropped as log-only, which reads like gate rejections in the funnel")
    if MAX_CANDIDATES_FOR_AI > 0 and AI_BATCH_MAX > 0 and AI_DAILY_BUDGET > 0:
        worst_case_requests_per_scan = max(1, -(-MAX_CANDIDATES_FOR_AI // AI_BATCH_MAX)) * 2
        if worst_case_requests_per_scan >= AI_DAILY_BUDGET:
            warnings.append(f"AI_DAILY_BUDGET ({AI_DAILY_BUDGET}) per provider is small enough "
                            f"that a single scan sending MAX_CANDIDATES_FOR_AI "
                            f"({MAX_CANDIDATES_FOR_AI}) could exhaust ONE provider by itself if "
                            "retries are needed — the pool will just move to the next provider, "
                            "but consider raising the budget or lowering MAX_CANDIDATES_FOR_AI")
    return warnings

CSV_COLUMNS = ["timestamp", "signal_id", "coin", "signal", "entry", "SL", "TP", "RR",
               "leverage", "position_size", "funding_rate", "confidence",
               "confluence", "score_1h", "score_15m", "score_5m",
                "sweep", "sweep_age", "rsi_bounce", "reason", "ai_used",
                "liquidation",
               "decision", "setup_quality", "htf_bias", "structure",
               "sr_zone", "liquidity", "no_trade_reason", "data_warnings"]

AI_OPINIONS_LOG_FILE = BASE_DIR / "ai_opinions.csv"
AI_OPINION_COLUMNS = ["timestamp", "scan_id", "signal_id", "symbol",
                      "deterministic_decision", "ai_opinion", "ai_status",
                      "ai_confidence", "ai_reason", "agreement", "final_decision"]


def setup_logging() -> None:
    """Configure root logging: console + rotating file. Never print()."""
    # A cp1252 Windows console cannot encode CJK/emoji symbols (Binance lists
    # coins like 牛来/龙虾); one bad character used to raise inside the handler
    # and drop the whole log line. Reconfigure stdout to UTF-8 when possible.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
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
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
