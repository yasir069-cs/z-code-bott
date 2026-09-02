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
# AI provider key. The historical name is OPENROUTER_API_KEY; AGENTROUTER_API_KEY
# is accepted as an alias so .env can carry the provider-accurate name.
OPENROUTER_API_KEY = (os.getenv("AGENTROUTER_API_KEY")
                      or os.getenv("OPENROUTER_API_KEY", "")).strip()

# ------------------------------------------------------------------ scanner
EXCHANGE_ID = "binance"
VOLUME_MIN_USDT = 50_000_000    # 24h quote volume filter ($50M — futures liquidity)
CANDLE_LIMIT = 50               # last 50 candles per timeframe (strategy window)
INDICATOR_WARMUP = 250          # extra closed candles so pandas-ta values converge
                                # to TradingView (Wilder/EMA recursions need warm-up)
# A brand-new perp cannot supply 50+250 candles. Skipping it for that (the old
# `len(rows) < 300` rule) made every fresh listing permanently untradeable — and
# logged a WARNING per scan, per coin (live 2026-09-02: MARSCOIN 26 rows,
# 牛来 72 rows). The structural core only needs pivots (8 bars) and the secondary
# indicators 25; a short frame is therefore served with its real length — values
# are simply less converged than TradingView's — and only frames shorter than
# this are dropped.
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

# Sweep is required for the STRONGEST alert tier: without it the confidence is
# capped just below ALERT_TIER_STRONG_MIN (70) and the alert is labelled
# "no sweep" — a no-sweep setup can reach HIGH but never STRONG.
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

# ------------------------------------------------------------------ AI (AgentRouter / DeepSeek v4)
# AI_BASE_URL keeps the transport configurable (any OpenAI-compatible
# provider). Primary: AgentRouter serving deepseek-v4-flash.
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://agentrouter.org/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-v4-flash")
# Secondary model, tried when the primary fails every retry. Empty by default:
# the AgentRouter key only serves the primary model. Set AI_MODEL_FALLBACK in
# .env when the provider offers a second usable model. Only after both paths
# fail does the run fall back to the pure-Python indicator decision.
AI_MODEL_FALLBACK = os.getenv("AI_MODEL_FALLBACK", "").strip()
AI_MAX_TOKENS = 2000            # was 300: truncated single answers mid-"reason"
                                # (finish_reason=length) and cannot hold a batch
AI_TIMEOUT_SECONDS = 60.0
# Bound for the interactive Telegram assistant, measured across its WHOLE retry
# ladder (not one request): a chat reply must not sit on "typing..." while the
# ladder works through 3 attempts x 2 models x 60s.
AI_CHAT_DEADLINE_SECONDS = 45.0
AI_TEMPERATURE = 0.1
# Reasoning is DISABLED on purpose: the model burns the token budget on
# chain-of-thought and the final JSON never gets produced (finish_reason=length).
# Verified via scripts/openrouter_diagnose.py (Test C works, Test D starves).
AI_REASONING_ENABLED = False

# Batching: one request carries every candidate from a scan and returns a JSON
# array. This collapses ~10s-per-candidate into a single round trip and keeps
# the request count low whatever the provider's daily cap is.
AI_BATCH_ENABLED = True
AI_BATCH_MAX = 20               # candidates per request; more than this is chunked
                                # (20 fits a full scan's shortlist in ONE request)
AI_RETRY_MAX = 3                # retry 429 / 5xx / timeout / malformed JSON
AI_RETRY_BACKOFF_BASE = 1.0     # 1s, 2s, 4s
AI_DAILY_BUDGET = int(os.getenv("AI_DAILY_BUDGET", "50"))  # advisory daily request cap

# ------------------------------------------------------------------ duplicate guard
DUPLICATE_COOLDOWN_MIN = 15     # same coin within 15 min -> skip (futures pace faster)
GUARD_RESET_TIME = "23:00"      # tracker resets at 11:00 PM IST
# A blocked setup is re-evaluated every scan, and each rejection used to append
# its own HOLD row: 37 blocked coins x 60 scans = ~2.2k rows a session, and an
# on-demand /scan_on session runs 24/7. HOLD rows still land in the CSV (the
# audit trail wants them) but only once per window per coin; a change of verdict
# or of the blocking reason is a new row.
HOLD_LOG_COOLDOWN_MIN = 30

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

# ---- hardening: bounded services, watchdogs, isolation
LIQ_STALE_SECONDS = 1800        # no forceOrder message for 30 min -> stream
                                # STALE (quiet markets go minutes between
                                # liquidations; shorter would false-alarm)
NEWS_429_COOLDOWN_SECONDS = 600     # per-feed backoff after HTTP 429
NEWS_TIMEOUT_COOLDOWN_SECONDS = 300  # per-feed backoff after timeout/conn error
AI_WORKER_MAX_PENDING = 2       # background AI queue depth; beyond it the
                                # oldest pending batch is dropped (UNAVAILABLE)

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
QUALITY_MIN = 50                 # final setup-quality gate for a tradable setup (owner's
                                 # tier system: below 50 is ignored, 50+ is alertable)

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
RISK_MIN_TARGET_ATR = 1.0        # no opposing zone offers this much reward * ATR -> NO_TRADE
                                # (judged per zone AFTER RISK_TARGET_ZONE_PAD_ATR, so a zone the
                                # target would land inside still counts as no room)
RISK_MAX_SPREAD_PCT = 0.0015     # spread wider than this (when known) -> NO_TRADE
RISK_TARGET_ZONE_PAD_ATR = 0.25  # target placed this * ATR short of the opposing zone edge
# Search every opposing S/R zone for a *reachable* target, not just the nearest
# one. A zone is a range ~1 ATR wide, so the nearest support/resistance often
# straddles the entry or sits a fraction of an ATR away: that is a wall, not a
# target, and treating it as the only option turned "no room to the first zone"
# into NO_TRADE while a usable zone several ATR deeper went unread (21/37 coins
# in the 2026-09-02 live scan died of `no_clear_target`/`target_too_close`).
# False restores the original nearest-zone-only behaviour.
RISK_TARGET_SCAN_ZONES = True

# ---- decision engine
# Master switch for the price-action core vs the legacy funnel scorer.
# Currently INERT: nothing reads it — `decision.decide` is unconditional and the
# legacy graded scorer survives only as `scoring.indicator_confirmation` (secondary
# layer) and in `backtest.py --strategy`. Kept as the documented kill-switch slot.
DECISION_ENABLED = True

# ---- LLM opinion AUDIT (background; can never change what is emitted)
# True: every shortlisted coin is queued to the worker with its full structured
# data and the answer lands in ai_opinions.csv (with an `agreement` grade) for the
# owner to read. It is NOT a decision stage — the scheduled scan has already
# emitted its verdict by the time the batch returns, and nothing re-reads it.
# Applying a verdict exists only behind `--force-llm` (see main.run_force_llm),
# where post-LLM gates re-validate it. False: no requests, no spend, no audit.
LLM_DECISION_ENABLED = True      # False -> no AI audit; alerts are identical
# ---- alert tier system (owner's rule, 2026-09-01) ----
# Below 50: ignored (log-only, never alerts). 50-60: NORMAL alert.
# 60-70: HIGH alert. 70-100: STRONGEST alert. The tier is read from the
# confidence the pipeline computed (setup-quality after the no-sweep cap).
ALERT_QUALITY_MIN = 50           # Telegram alert floor; a BUY/SELL below this is log-only
ALERT_TIER_NORMAL_MIN = 50       # 50.0-59.9 -> NORMAL
ALERT_TIER_HIGH_MIN = 60         # 60.0-69.9 -> HIGH
ALERT_TIER_STRONG_MIN = 70       # 70.0+     -> STRONG

# ---- news verification engine (VERIFY FIRST — AI never decides what is true)
NEWS_ENABLED = False             # news engine OFF (owner's call, 2026-08-30)
NEWS_POLL_SECONDS = 300          # RSS polling interval
NEWS_ALERT_COOLDOWN_SECONDS = 1200  # no duplicate/repeat alert for the same event within 20 min
NEWS_EVENT_WINDOW_HOURS = 24     # articles older than this cannot join/confirm an event
NEWS_MAX_ARTICLES_PER_CYCLE = 30
NEWS_SIMILARITY_MIN = 0.35       # token-Jaccard threshold for "same event" clustering
NEWS_MATERIAL_TOKEN_FRAC = 0.30  # new claim tokens above this fraction = material update
NEWS_ALLOW_UPDATE_ALERTS = False  # owner's rule: a news story alerts EXACTLY once;
                                  # flip True for 🔄 update alerts on material changes
NEWS_ALERT_MEMORY_DAYS = 7        # remember already-alerted news this long (restart-safe)
NEWS_ALERT_MEMORY_FILE = BASE_DIR / "news_alerted.json"
NEWS_AI_DAILY_LIMIT = 100        # separate from the trading-prompt budget
NEWS_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    # Trump / US-politics crypto feeds: Trump policy news moves BTC and the
    # broader market, so it gets dedicated discovery feeds.
    "https://cointelegraph.com/rss/tag/donald-trump",
    "https://cointelegraph.com/rss/tag/politics",
    # OFFICIAL announcement feeds (primary sources: an announcement here +
    # one independent news report = VERIFIED). Binance's announcement RSS is
    # bot-blocked (HTTP 202/empty), so press + regulator coverage is the
    # practical path for exchange news today.
    "https://blog.ethereum.org/feed.xml",
    "https://solana.com/news/rss.xml",
    # social/unofficial feeds: discovery + early clustering only — they NEVER
    # satisfy verification (see NEWS_SOCIAL_DOMAINS and compute_status)
    "https://www.reddit.com/r/CryptoCurrency/.rss",
    "https://www.reddit.com/r/Bitcoin/.rss",
    "https://www.reddit.com/r/ethereum/.rss",
)
# Domains that count as PRIMARY/official sources (exchange, regulator, project).
# An event is VERIFIED only with one of these PLUS an independent confirmation.
NEWS_OFFICIAL_DOMAINS = frozenset({
    "binance.com", "coinbase.com", "kraken.com", "okx.com", "bybit.com",
    "sec.gov", "treasury.gov", "federalreserve.gov", "ecb.europa.eu",
    "ethereum.org", "bitcoin.org", "solana.com", "ripple.com",
})
# Social/community domains: real-time "social truth" for discovery, but a
# social post is a rumor, not evidence — it can never confirm an event.
NEWS_SOCIAL_DOMAINS = frozenset({
    "reddit.com", "old.reddit.com", "np.reddit.com",
    "twitter.com", "x.com", "nitter.net",
})

# ------------------------------------------------------------------ files
LIQUIDATION_RECONNECT_SECONDS = 5
LIQUIDATION_BURST_COUNT = 3
# Lookback windows (name -> seconds) for the websocket liquidation summary.
# The cache keeps events for the largest window; summaries expose one block
# per window with long/short notional+count, latest event time, burst flag,
# price-vs-current context and freshness.
LIQUIDATION_WINDOWS = {"5m": 300, "15m": 900, "1h": 3600}
SIGNALS_LOG_FILE = BASE_DIR / "signals_log.csv"
BOT_LOG_FILE = BASE_DIR / "bot.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Credentials known to have been exposed in chat/logs. If one of these is
# still configured at startup, warn loudly on every boot until rotated.
_EXPOSED_TELEGRAM_TOKEN = "8851597372:AAFlynes"
_EXPOSED_OPENROUTER_KEY = "sk-or-v1-5e4bb826af4b"


def check_exposed_credentials() -> list[str]:
    """Return warnings for any still-configured credential that is known to
    have been leaked publicly. Empty list = clean."""
    warnings = []
    if TELEGRAM_TOKEN.startswith(_EXPOSED_TELEGRAM_TOKEN):
        warnings.append("TELEGRAM_TOKEN was exposed in chat — revoke it via "
                        "@BotFather /revoke and put the new token in .env")
    if OPENROUTER_API_KEY.startswith(_EXPOSED_OPENROUTER_KEY):
        warnings.append("OPENROUTER_API_KEY was exposed in chat — revoke it at "
                        "the provider's key page and put the new key in .env")
    return warnings

CSV_COLUMNS = ["timestamp", "signal_id", "coin", "signal", "entry", "SL", "TP", "RR",
               "leverage", "position_size", "funding_rate", "confidence",
               "confluence", "score_1h", "score_15m", "score_5m",
                "sweep", "sweep_age", "rsi_bounce", "reason", "ai_used",
                "liquidation",
               # structure-first decision core (Phase 6): the primary evidence
               "decision", "setup_quality", "htf_bias", "structure",
               "sr_zone", "liquidity", "no_trade_reason", "data_warnings"]

# Background AI opinions audit log (signal_id keyed; never blocks a scan)
AI_OPINIONS_LOG_FILE = BASE_DIR / "ai_opinions.csv"
# `agreement` grades the AI opinion against the verdict actually emitted, so the
# audit file answers "would the model have changed anything?" at a glance:
# AGREE / DISAGREE / VETO_PROPOSED (model would suppress a Python signal) /
# SIGNAL_PROPOSED (model wants a trade Python blocked) / NO_ANSWER.
# final_decision stays the deterministic verdict: the stage is audit-only and can
# never replace what shipped.
AI_OPINION_COLUMNS = ["timestamp", "scan_id", "signal_id", "symbol",
                      "deterministic_decision", "ai_opinion", "ai_status",
                      "ai_confidence", "ai_reason", "agreement", "final_decision"]


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
