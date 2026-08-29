"""News verification engine — deterministic verification, AI strictly downstream.

Pipeline (the AI NEVER decides whether news is true):

    discovery -> source verification -> [VERIFIED] -> AI summary
             -> AI market-impact analysis -> Telegram

Truth comes from sources: an event is VERIFIED only when a primary/official
source states the claim AND an independent outlet confirms it. Only VERIFIED
events may reach the AI stage or Telegram; DEVELOPING / PARTIALLY_VERIFIED /
CONFLICTED / DEBUNKED events are retained in state (so later confirmations
can upgrade them) but are never published.

News never touches the trading pipeline: this module produces alerts only
and never imports decision/signal code.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests

import config

log = logging.getLogger("news")

# statuses, in precedence order (highest first)
DEBUNKED = "DEBUNKED"
CONFLICTED = "CONFLICTED"
VERIFIED = "VERIFIED"
PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
DEVELOPING = "DEVELOPING"
UNVERIFIED = "UNVERIFIED"

_RETRACTION_MARKERS = ("retraction", "retracts", "retracted", "debunk",
                       "denies", "denied", "false report", "fake news")

_STOPWORDS = frozenset("""
a an and are as at be but by for from has have in is it its of on or that the
this to was were will with after before over under new says said reports
report according amid amid among
""".split())

_NAME_TO_ASSET = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "binance": "BNB",
    "ripple": "XRP", "xrp": "XRP", "dogecoin": "DOGE", "cardano": "ADA",
    "avalanche": "AVAX", "polkadot": "DOT", "chainlink": "LINK",
    "litecoin": "LTC", "tether": "USDT", "usdc": "USDC", "tron": "TRX",
    "shiba inu": "SHIB", "pepe": "PEPE", "sui": "SUI", "aptos": "APT",
    # Trump news moves both the TRUMP token and the broad market (the AI's
    # cross-market analysis decides which); extraction stays literal.
    "trump": "TRUMP", "donald trump": "TRUMP", "world liberty financial": "WLF",
}
_TICKER_RE = re.compile(r"\$([A-Za-z]{2,10})\b")

# Entity anchors: exchange / project / regulator roots. Two articles sharing
# an anchor AND some topical overlap belong to the same story arc even when
# the headlines are heavily paraphrased.
_ANCHORS = frozenset(
    d.split(".")[0].removeprefix("www.") for d in config.NEWS_OFFICIAL_DOMAINS
) | frozenset(_NAME_TO_ASSET.keys())
_ANCHOR_MERGE_FLOOR = 0.10        # min topical overlap when an anchor is shared


@dataclass
class Article:
    title: str
    url: str
    source_domain: str
    published_at: datetime
    summary: str = ""

    def claim_tokens(self) -> frozenset:
        return frozenset(_tokens(self.title + " " + self.summary))


@dataclass
class NewsEvent:
    articles: list = field(default_factory=list)
    assets: set = field(default_factory=set)
    status: str = DEVELOPING
    verified_at: Optional[datetime] = None
    last_alerted_at: Optional[datetime] = None
    alerted_claim_tokens: Optional[frozenset] = None
    alerted_official: bool = False

    @property
    def primary_source(self) -> Optional[Article]:
        officials = [a for a in self.articles if is_official(a.source_domain)]
        return officials[0] if officials else None

    @property
    def independent_confirmations(self) -> list:
        """Non-social independent sources (for the alert's Sources section);
        a social post is never listed as a 'confirmation'."""
        primary = self.primary_source
        seen, out = set(), []
        for a in self.articles:
            if primary and a.url == primary.url:
                continue
            if is_social(a.source_domain):
                continue
            dom = a.source_domain
            if dom not in seen:
                seen.add(dom)
                out.append(a)
        return out

    @property
    def headline(self) -> str:
        return max(self.articles, key=lambda a: a.published_at).title

    def claim_tokens(self) -> frozenset:
        union = set()
        for a in self.articles:
            union |= a.claim_tokens()
        return frozenset(union)


def _stem(w: str) -> str:
    """Crude plural strip so paraphrases match: 'withdrawals' == 'withdrawal',
    'halts' == 'halt'. Length-guarded to keep 'press'/'btc' intact."""
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _tokens(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {_stem(w) for w in words if len(w) > 2 and w not in _STOPWORDS}


def _parse_iso(raw) -> datetime:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def is_official(domain: str) -> bool:
    # NOTE: strip ONLY the exact "www." prefix. An earlier version used
    # lstrip("www."), which removes ANY leading w/. characters — that let
    # phishing look-alikes (wbinance.com, .binance.com) pass as official
    # primary sources in verification.
    d = (domain or "").lower().strip().removeprefix("www.")
    if not d or d.startswith(".") or ".." in d:
        return False                      # malformed / empty-label domain
    return any(d == od or d.endswith("." + od) for od in config.NEWS_OFFICIAL_DOMAINS)


def is_social(domain: str) -> bool:
    """Social/community source (Reddit, X, ...). Social posts are rumors for
    discovery; they never count as evidence in verification."""
    d = (domain or "").lower().strip().removeprefix("www.")
    return any(d == sd or d.endswith("." + sd) for sd in config.NEWS_SOCIAL_DOMAINS)


def extract_assets(text: str) -> set:
    found = set(_TICKER_RE.findall(text))
    low = text.lower()
    for name, ticker in _NAME_TO_ASSET.items():
        if name in low:
            found.add(ticker)
    return {a.upper() for a in found}


def _similarity(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _parse_pubdate(raw: str) -> datetime:
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------- discovery

def _atom_entries(root) -> list:
    """Atom feeds (Reddit .rss) use <entry>/<link href>/<updated> instead of
    RSS 2.0's <item>/<link>/<pubDate>."""
    ns = "{http://www.w3.org/2005/Atom}"
    entries = list(root.iter(f"{ns}entry"))
    if not entries:
        entries = list(root.iter("entry"))       # namespace-less Atom
    return entries


def _atom_fields(entry) -> tuple:
    ns = "{http://www.w3.org/2005/Atom}"
    title = ((entry.findtext(f"{ns}title") or entry.findtext("title") or "")).strip()
    link = ""
    for l in list(entry.iter(f"{ns}link")) + list(entry.iter("link")):
        href = l.get("href") or l.get("href")
        if href:
            link = href.strip()
            break
    updated = (entry.findtext(f"{ns}updated") or entry.findtext("updated")
               or entry.findtext(f"{ns}published") or entry.findtext("published") or "")
    summary = (entry.findtext(f"{ns}content") or entry.findtext("content")
               or entry.findtext(f"{ns}summary") or entry.findtext("summary") or "")
    return title, link, updated, summary


def fetch_rss_articles(feeds=config.NEWS_RSS_FEEDS,
                       limit=config.NEWS_MAX_ARTICLES_PER_CYCLE,
                       timeout=15.0) -> list:
    """Pull articles from the configured public RSS/Atom feeds. Never raises.

    Handles both RSS 2.0 (CoinDesk/Cointelegraph <item>) and Atom (Reddit
    .rss <entry>) so social discovery feeds work without extra deps.
    `limit` applies PER FEED so later feeds (Trump/politics/social) are not
    starved by the first big feed hitting a global cap."""
    articles: list[Article] = []
    for feed in feeds:
        feed_count = 0
        try:
            resp = requests.get(feed, timeout=timeout,
                                headers={"User-Agent": "crypto-signal-bot/1.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            items = []
            for item in root.iter("item"):            # RSS 2.0
                items.append(((item.findtext("title") or "").strip(),
                              (item.findtext("link") or "").strip(),
                              item.findtext("pubDate") or "",
                              item.findtext("description") or ""))
            for entry in _atom_entries(root):         # Atom (social feeds)
                items.append(_atom_fields(entry))

            for title, url, pub, summary in items:
                if not title or not url:
                    continue
                articles.append(Article(
                    title=title, url=url,
                    source_domain=urlparse(url).netloc or urlparse(feed).netloc,
                    published_at=_parse_pubdate(pub),
                    summary=_strip_html(summary)[:500],
                ))
                feed_count += 1
                if feed_count >= limit:
                    break
        except Exception as exc:
            log.warning("RSS feed %s failed: %s", feed, exc)
    return articles


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text).strip()


# ------------------------------------------------------------- verification

def compute_status(event: NewsEvent) -> str:
    """Deterministic verification verdict. The AI is NEVER consulted here.

    Social posts are discovery-only: they are excluded from corroboration,
    so no number of Reddit/X posts can lift an event above DEVELOPING —
    only an official source + an independent news outlet can VERIFY."""
    titles = " ".join(a.title.lower() for a in event.articles)
    official_denial = any(is_official(a.source_domain) and any(m in a.title.lower()
                        for m in _RETRACTION_MARKERS) for a in event.articles)
    if official_denial:
        return DEBUNKED
    if any(m in titles for m in _RETRACTION_MARKERS):
        return CONFLICTED

    has_official = any(is_official(a.source_domain) for a in event.articles)
    # corroboration counts only non-social domains
    news_domains = {a.source_domain for a in event.articles
                    if not is_social(a.source_domain)}

    if has_official and len(news_domains) >= 2:
        return VERIFIED                # primary source + independent confirmation
    if len(news_domains) >= 2:
        return PARTIALLY_VERIFIED      # corroborated, but no primary source
    return DEVELOPING


def format_news_alert(event: NewsEvent, analysis: dict, market: dict,
                      update_note: Optional[str] = None) -> str:
    """Render the Telegram message for a VERIFIED event (spec template)."""
    direction = analysis.get("direction", "UNCERTAIN")
    strength = analysis.get("strength", "LOW")
    horizons = analysis.get("horizons") or {}
    primary = event.primary_source
    independents = event.independent_confirmations
    sources_lines = []
    if primary:
        sources_lines.append(primary.url)
    if independents:
        sources_lines.append(independents[0].url)

    lines = [
        "🔄 VERIFIED NEWS UPDATE" if update_note else "🟢 VERIFIED CRYPTO NEWS",
        "",
        f"📰 {event.headline}",
    ]
    if update_note:
        lines.append("")
        lines.append(f"♻️ What changed: {update_note}")
    lines += [
        "",
        "📌 What happened",
        analysis.get("summary", ""),
        "",
        "📊 Market impact",
        direction,
        "",
        "⚡ Impact strength",
        strength,
        "",
        "⏱ Time horizon",
        f"Immediate: {horizons.get('immediate', 'n/a')}",
        f"Short-term: {horizons.get('short_term', 'n/a')}",
        f"Medium-term: {horizons.get('medium_term', 'n/a')}",
        "",
        "🤖 AI market analysis",
        analysis.get("interpretation", ""),
        "",
        "🪙 Affected assets",
        ", ".join(sorted(event.assets)) or "n/a",
        "",
        "⚠️ Key risk",
        analysis.get("key_risk", "n/a"),
        "",
        "✅ Verification",
        "VERIFIED",
        "",
        "🔗 Sources",
        "\n".join(sources_lines) or "n/a",
    ]

    observed = market.get("observed") or []
    if observed:
        def _pct(v):
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else "n/a"

        obs_lines = ["", "📉 Observed market data (measured, not interpretation)"]
        for o in observed:
            price = o.get("price")
            price_s = f"{price:.6g}" if isinstance(price, (int, float)) else "n/a"
            obs_lines.append(
                f"{o.get('symbol', '?')}: price {price_s}, "
                f"24h {_pct(o.get('change_24h_pct'))}, "
                f"1h {_pct(o.get('change_1h_pct'))}")
        lines += obs_lines
    elif market.get("unavailable"):
        lines += ["", "📉 Market data unavailable at processing time"]

    return "\n".join(lines)


# ------------------------------------------------------------------- engine

class AlertMemory:
    """Persistent memory of already-alerted news (claim-token signatures).

    The owner's rule is absolute: news that has alerted once never alerts
    again. Per-event state cannot enforce that across the paths where the
    same story returns as a *fresh* event:

      * a bot restart (RSS feeds still list the old articles; seen_urls is
        empty again),
      * the 24h event window expiring while outlets keep re-reporting,
      * heavy paraphrasing clustering into a second event.

    This cross-event, restart-persistent memory catches all three: before a
    'new' alert is offered, its claim tokens are matched (clustering-grade
    similarity + shared entity anchor) against everything ever alerted."""

    def __init__(self, path):
        self._path = Path(path)
        self._entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = [e for e in raw.get("entries", [])
                             if isinstance(e, dict) and e.get("tokens")]
        except FileNotFoundError:
            self._entries = []
        except (OSError, ValueError) as exc:
            log.warning("Alert memory unreadable (%s); starting fresh", exc)
            self._entries = []
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.NEWS_ALERT_MEMORY_DAYS)
        self._entries = [e for e in self._entries if _parse_iso(e.get("at")) > cutoff]

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps({"entries": self._entries}, ensure_ascii=False),
                encoding="utf-8")
        except OSError as exc:
            log.warning("Alert memory write failed: %s", exc)

    def contains(self, tokens: frozenset) -> Optional[str]:
        """Headline of an already-alerted story matching these tokens.
        Stale entries are pruned first so expired memories never suppress."""
        self._prune()
        for e in self._entries:
            et = frozenset(e["tokens"])
            best = _similarity(tokens, et)
            shared_anchor = bool(tokens & _ANCHORS & et)
            if best >= config.NEWS_SIMILARITY_MIN or \
                    (shared_anchor and best >= _ANCHOR_MERGE_FLOOR):
                return e.get("headline", "")
        return None

    def add(self, tokens: frozenset, headline: str) -> None:
        self._entries.append({"tokens": sorted(tokens), "headline": headline,
                              "at": datetime.now(timezone.utc).isoformat()})
        self._prune()
        self._save()


class NewsEngine:
    """Clusters articles into events, verifies them deterministically, and
    publishes only VERIFIED events (via the AI analysis + Telegram)."""

    def __init__(self, fetch_fn: Optional[Callable[[], list]] = None):
        self._events: list[NewsEvent] = []
        self._fetch_fn = fetch_fn or fetch_rss_articles
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_urls: set = set()
        self._alerts = AlertMemory(config.NEWS_ALERT_MEMORY_FILE)

    # -- ingestion & clustering -------------------------------------------
    def ingest(self, articles: list) -> list:
        """Add articles to events; return (event, kind) pairs the caller must
        consider publishing: kind is 'new' (freshly VERIFIED) or 'update'
        (material new verified information on an already-alerted event)."""
        now = datetime.now(timezone.utc)
        horizon = timedelta(hours=config.NEWS_EVENT_WINDOW_HOURS)
        self._events = [e for e in self._events
                        if e.articles and max(a.published_at for a in e.articles)
                        > now - horizon]

        pending: list[tuple[NewsEvent, str]] = []
        for art in articles:
            if not art or art.url in self._seen_urls:
                continue
            self._seen_urls.add(art.url)

            target = None
            art_tokens = art.claim_tokens()
            for event in self._events:
                best = max(_similarity(art_tokens, a.claim_tokens())
                           for a in event.articles)
                shared_anchor = bool(art_tokens & _ANCHORS
                                     & event.claim_tokens())
                if best >= config.NEWS_SIMILARITY_MIN or \
                        (shared_anchor and best >= _ANCHOR_MERGE_FLOOR):
                    target = event
                    break
            if target is None:
                target = NewsEvent()
                self._events.append(target)

            target.articles.append(art)
            target.assets |= extract_assets(art.title + " " + art.summary)
            target.status = compute_status(target)

            if target.status == VERIFIED and target.last_alerted_at is None:
                # Owner's rule: news alerts exactly once. The persistent
                # alert memory catches the same story returning as a fresh
                # cluster (restart, expired window, paraphrase).
                dup = self._alerts.contains(target.claim_tokens())
                if dup:
                    log.info("News suppressed — already alerted before: '%s'",
                             (dup or target.headline)[:70])
                else:
                    pending.append((target, "new"))
            elif target.status == VERIFIED and target.last_alerted_at is not None:
                # Re-alerts (material updates) are OFF by default per the
                # owner's once-only rule; available behind the config flag.
                if config.NEWS_ALLOW_UPDATE_ALERTS:
                    in_cooldown = (now - target.last_alerted_at).total_seconds() \
                        < config.NEWS_ALERT_COOLDOWN_SECONDS
                    if not in_cooldown:
                        note = self._material_update_note(target)
                        if note:
                            pending.append((target, "update"))
        return pending

    def _material_update_note(self, event: NewsEvent) -> Optional[str]:
        """Detect materially important new verified information on an
        already-alerted event. Cosmetic re-reports never re-alert."""
        old = event.alerted_claim_tokens or frozenset()
        new_tokens = event.claim_tokens() - old
        union = event.claim_tokens() | old
        new_official = any(is_official(a.source_domain) for a in event.articles) \
            and not event.alerted_official
        if union and len(new_tokens) / len(union) >= config.NEWS_MATERIAL_TOKEN_FRAC:
            return "new verified details: " + ", ".join(sorted(new_tokens)[:6])
        if new_official:
            return "the primary/official source has now confirmed the event"
        return None

    # -- publishing --------------------------------------------------------
    def publish(self, event: NewsEvent, kind: str, analyzer, market_fn,
                send_fn) -> bool:
        """Run AI analysis on a VERIFIED event and send the Telegram alert.

        Failure behavior: if the AI analysis fails, NOTHING is sent — no
        incomplete message. Market-data failure only omits those fields."""
        if event.status != VERIFIED:
            return False                       # AI can never publish unverified news
        market = {"observed": [], "unavailable": ["all"]}
        try:
            if market_fn is not None:
                market = market_fn(sorted(event.assets))
        except Exception as exc:
            log.warning("News market-data lookup failed: %s", exc)

        update_note = None
        if kind == "update":
            update_note = self._material_update_note(event)
            if not update_note:
                return False
        try:
            analysis = analyzer(event, market)
        except Exception as exc:
            log.warning("News AI analysis failed for '%s': %s", event.headline, exc)
            analysis = None
        if not analysis:
            log.info("News AI analysis unavailable — skipping alert for '%s' "
                     "(no incomplete messages)", event.headline)
            return False

        message = format_news_alert(event, analysis, market, update_note)
        sent = bool(send_fn(message))
        if sent:
            event.last_alerted_at = datetime.now(timezone.utc)
            event.alerted_claim_tokens = event.claim_tokens()
            event.alerted_official = any(is_official(a.source_domain)
                                         for a in event.articles)
            # persistent once-only memory: survives restarts and re-clustering
            self._alerts.add(event.claim_tokens(), event.headline)
        return sent

    def process_cycle(self, analyzer=None, market_fn=None, send_fn=None) -> list:
        """One discovery->verify->analyze->publish cycle. Returns sent messages."""
        import alerts
        import news_analysis
        import news_market

        analyzer = analyzer or news_analysis.analyze_event
        market_fn = market_fn or news_market.observe
        send_fn = send_fn or alerts.send_telegram_text

        pending = self.ingest(self._fetch_fn())
        sent = []
        for event, kind in pending:
            if self.publish(event, kind, analyzer, market_fn, send_fn):
                sent.append(event.headline)
        if pending:
            log.info("News cycle: %d candidate(s), %d alert(s) sent",
                     len(pending), len(sent))
        return sent

    # -- background thread --------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="NewsEngine")
        self._thread.start()
        log.info("News verification engine started (verify first, AI analyzes later)")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_cycle()
            except Exception as exc:
                log.warning("News engine cycle failed: %s", exc)
            self._stop.wait(config.NEWS_POLL_SECONDS)


_engine = NewsEngine()


def start_engine() -> None:
    _engine.start()
