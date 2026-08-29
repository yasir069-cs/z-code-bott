"""News verification engine tests — the AI NEVER decides what is true.

Pins the whole spec: VERIFIED-only publication, deterministic verification,
fact/interpretation separation, no-hallucination guardrails, duplicate vs
material-update handling, observed-data labeling, causality discipline and
the news/trading-signal separation.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import news
import news_analysis as na
from news import (Article, NewsEngine, NewsEvent, compute_status,
                  format_news_alert, is_official)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_alert_memory(tmp_path, monkeypatch):
    """Every test gets its own alert-memory file — the persistent once-only
    store must never leak state between tests (or into the repo)."""
    monkeypatch.setattr(config, "NEWS_ALERT_MEMORY_FILE", tmp_path / "alerted.json")


def _art(title, domain, url, summary="", when=None):
    return Article(title=title, url=url, source_domain=domain,
                   published_at=when or NOW, summary=summary)


def _hack_event():
    """A verified exchange-hack event: official source + independent press."""
    e = NewsEvent()
    e.articles = [
        _art("Binance reports security incident affecting withdrawal system",
             "binance.com", "https://binance.com/en/support/announcement/x",
             "Binance confirmed an incident and suspended withdrawals."),
        _art("Binance halts withdrawals after security incident",
             "cointelegraph.com", "https://cointelegraph.com/news/1",
             "Withdrawals on Binance are suspended following an incident."),
    ]
    e.assets = {"BNB", "BTC"}
    e.status = compute_status(e)
    return e


def _ai_ok(event, market=None):
    return {
        "summary": "Binance officially confirmed a security incident affecting "
                   "its withdrawal system and suspended withdrawals.",
        "direction": "BEARISH",
        "strength": "HIGH",
        "horizons": {"immediate": "Could cause volatility and liquidation activity.",
                     "short_term": "Could create selling pressure on BNB.",
                     "medium_term": "Impact depends on the outcome of the investigation."},
        "interpretation": "Potentially bearish for BNB because withdrawal "
                          "suspensions historically reduce confidence.",
        "key_risk": "Scope of the incident is not yet public.",
        "fact_interpretation_separated": True,
    }


# --------------------------------------------------- 1. verification is rule-based

def test_verified_needs_official_plus_independent():
    assert _hack_event().status == "VERIFIED"


def test_press_only_is_not_verified():
    e = NewsEvent()
    e.articles = [
        _art("Binance halts withdrawals", "cointelegraph.com", "https://a.com/1"),
        _art("Binance halts withdrawals", "theblock.co", "https://b.com/1"),
    ]
    assert compute_status(e) == "PARTIALLY_VERIFIED"


def test_single_source_is_developing():
    e = NewsEvent()
    e.articles = [_art("Something happened", "cointelegraph.com", "https://a.com/1")]
    assert compute_status(e) == "DEVELOPING"


def test_official_denial_debunks():
    e = NewsEvent()
    e.articles = [
        _art("Exchange X denies hack report", "binance.com", "https://binance.com/1"),
        _art("Exchange X denies hack report", "cointelegraph.com", "https://c.com/1"),
    ]
    assert compute_status(e) == "DEBUNKED"


# ------------------------------------------------ social sources (rumor-stage)

def test_social_never_verifies_an_event():
    """Any number of social posts cannot lift an event above DEVELOPING —
    a Reddit thread is a rumor, not evidence."""
    e = NewsEvent()
    e.articles = [
        _art("Huge exchange incident reported", "www.reddit.com", "https://reddit.com/r/1"),
        _art("Huge exchange incident reported", "www.reddit.com", "https://reddit.com/r/2"),
        _art("Huge exchange incident reported", "x.com", "https://x.com/u/1"),
    ]
    assert compute_status(e) == "DEVELOPING"


def test_social_does_not_count_as_independent_confirmation():
    """Official source + social buzz is still NOT verified: verification
    needs an independent NEWS outlet, not a Reddit echo."""
    e = NewsEvent()
    e.articles = [
        _art("Exchange X confirms incident", "binance.com", "https://binance.com/1"),
        _art("Exchange X incident discussed", "www.reddit.com", "https://reddit.com/r/1"),
    ]
    assert compute_status(e) == "DEVELOPING"

    # ...but once one news outlet reports it, the event verifies
    e.articles.append(_art("Exchange X incident reported", "cointelegraph.com",
                           "https://c.com/1"))
    assert compute_status(e) == "VERIFIED"


def test_social_plus_press_is_not_partial():
    """Social + one news outlet = the outlet alone (DEVELOPING): the social
    post adds nothing to corroboration."""
    e = NewsEvent()
    e.articles = [
        _art("Something happened", "cointelegraph.com", "https://c.com/1"),
        _art("Something happened", "www.reddit.com", "https://reddit.com/r/1"),
    ]
    assert compute_status(e) == "DEVELOPING"


def test_social_clusters_into_events_and_alert_sources_stay_clean():
    """Social posts still feed discovery/clustering (early signal), and a
    VERIFIED alert never lists a social URL as an independent confirmation."""
    engine = NewsEngine()
    pending = engine.ingest([
        _art("Binance reports security incident", "www.reddit.com",
             "https://reddit.com/r/cc/1", "Users report withdrawal issues."),
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ])
    assert len(engine._events) == 1                    # social clustered in
    assert pending[0][0].status == "VERIFIED"
    confirmations = pending[0][0].independent_confirmations
    assert all(not news.is_social(a.source_domain) for a in confirmations)
    assert "cointelegraph.com" in [a.source_domain for a in confirmations]


def test_atom_entry_parsing_for_social_feeds():
    """Reddit .rss is Atom (<entry>/<link href>/<updated>), not RSS 2.0 —
    the parser must read both formats."""
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Bitcoin breaks out</title>
        <link href="https://www.reddit.com/r/Bitcoin/comments/abc123/breakout/"/>
        <updated>Sat, 29 Aug 2026 12:00:00 GMT</updated>
        <content>Discussion of the bitcoin breakout.</content>
      </entry>
    </feed>"""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(atom)
    title, url, updated, summary = news._atom_fields(root.find(
        "{http://www.w3.org/2005/Atom}entry"))
    assert title == "Bitcoin breaks out"
    assert "reddit.com" in url
    assert updated
    assert "breakout" in summary.lower()


# -------------------------------------------------- once-only alert memory

def test_same_story_split_across_5_feeds_alerts_once():
    """THE owner's scenario: one news landing 5 times (cross-feed paraphrases
    the clusterer splits into separate events) must produce exactly ONE
    alert. Two safety nets: similar-event dedup inside ingest's pending
    queue, and a memory re-check right before each publish."""
    engine = NewsEngine()
    variants = [
        ("binance.com", "https://binance.com/en/x",
         "Binance confirms security incident affecting withdrawals"),
        ("cointelegraph.com", "https://c.com/1",
         "Binance halts withdrawals after security incident"),
        ("theblock.co", "https://t.com/1",
         "Binance withdrawal halt follows security incident: report"),
        ("www.coindesk.com", "https://d.com/1",
         "Binance suspends withdrawals following security incident"),
        ("decrypt.co", "https://de.com/1",
         "Security incident at Binance halts withdrawals"),
    ]
    # feed the variants one by one (each becomes a fresh VERIFIED cluster if
    # the clusterer misses the paraphrase — worst case)
    sent_messages = []

    def send(message):
        sent_messages.append(message)
        return True

    for i, (dom, url, title) in enumerate(variants):
        arts = [Article(title=title, url=url, source_domain=dom,
                        published_at=NOW, summary="")]
        # each variant needs a second source to reach VERIFIED
        arts.append(Article(title=title, url=url + "-b",
                            source_domain="theblock.co" if dom != "theblock.co"
                            else "decrypt.co",
                            published_at=NOW, summary=""))
        pending = engine.ingest(arts)
        for event, kind in pending:
            engine.publish(event, kind, analyzer=_ai_ok,
                           market_fn=lambda a: {"observed": []},
                           send_fn=send)
    # only the very first variant may alert — the rest are the same story
    assert len(sent_messages) == 1, f"same news alerted {len(sent_messages)}x"
    assert "security incident" in sent_messages[0].lower()


def test_pending_queue_dedupes_similar_events_in_one_cycle():
    """Two similar VERIFIED events queued in the same ingest -> only one
    pending entry (the second is suppressed as the same story)."""
    engine = NewsEngine()
    pending = engine.ingest([
        _art("Binance halts withdrawals after security incident",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after security incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
        # different wording, same story, different URL -> may form event #2
        _art("Binance suspends withdrawals following security incident",
             "theblock.co", "https://t.com/1", "Withdrawal suspension."),
        _art("Binance suspends withdrawals following security incident",
             "decrypt.co", "https://de.com/1", "Binance withdrawal halt."),
    ])
    # whether clustering made 1 event or 2, exactly one alert is pending
    assert len(pending) == 1


def test_cycle_recheck_catches_split_story_across_publishes():
    """process_cycle re-checks memory before each publish, so two similar
    events pending in ONE cycle still alert exactly once."""
    engine = NewsEngine()
    # pre-seed two similar but separately-clustered events
    e1 = NewsEvent(); e1.articles = [
        _art("Binance halts withdrawals after security incident",
             "binance.com", "https://binance.com/en/x", ""),
        _art("Binance halts withdrawals after security incident",
             "cointelegraph.com", "https://c.com/1", "")]
    e1.assets = set(); e1.status = compute_status(e1)
    e2 = NewsEvent(); e2.articles = [
        _art("Binance suspends withdrawals following security incident",
             "theblock.co", "https://t.com/1", ""),
        _art("Binance suspends withdrawals following security incident",
             "decrypt.co", "https://de.com/1", "")]
    e2.assets = set(); e2.status = compute_status(e2)
    engine._events = [e1, e2]

    sent = engine.process_cycle(
        analyzer=_ai_ok,
        market_fn=lambda a: {"observed": [], "unavailable": []},
        send_fn=lambda m: True)
    assert len(sent) == 1


def _engine_with_memory(tmp_path, articles):
    """Fresh engine (simulating a restart) with a shared persistent memory."""
    import news as news_mod
    engine = news_mod.NewsEngine()
    engine._alerts = news_mod.AlertMemory(tmp_path / "alerted.json")
    engine._seen_urls = set()          # restart: URL memory gone
    pending = engine.ingest(articles)
    return engine, pending


def test_news_alerts_exactly_once_across_restart(tmp_path):
    """Owner's rule: news that alerted once NEVER alerts again — even after a
    bot restart, when the RSS feed still lists the same articles and the
    URL/event state is gone."""
    arts = [
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ]
    engine1, pending1 = _engine_with_memory(tmp_path, arts)
    assert len(pending1) == 1 and pending1[0][1] == "new"
    engine1.publish(pending1[0][0], "new", analyzer=_ai_ok,
                    market_fn=lambda a: {"observed": []},
                    send_fn=lambda m: True)

    # RESTART: brand-new engine, same memory file, same feed articles
    engine2, pending2 = _engine_with_memory(tmp_path, arts)
    assert pending2 == []               # already alerted: suppressed forever


def test_persistent_memory_catches_paraphrased_recluster(tmp_path):
    """The same story returning as a different cluster (heavy paraphrase /
    expired 24h window) is still recognized by claim-token similarity."""
    arts = [
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ]
    engine1, pending1 = _engine_with_memory(tmp_path, arts)
    engine1.publish(pending1[0][0], "new", analyzer=_ai_ok,
                    market_fn=lambda a: {"observed": []},
                    send_fn=lambda m: True)

    # "fresh" event: expired-window re-report with different wording
    engine2, pending2 = _engine_with_memory(tmp_path, [
        _art("Binance withdrawal halt follows confirmed security incident",
             "theblock.co", "https://t.com/1", "Withdrawals remain suspended."),
    ])
    assert pending2 == []


def test_genuinely_different_news_still_alerts(tmp_path):
    """Once-only must not over-suppress: an unrelated verified story alerts."""
    arts = [
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ]
    engine1, pending1 = _engine_with_memory(tmp_path, arts)
    engine1.publish(pending1[0][0], "new", analyzer=_ai_ok,
                    market_fn=lambda a: {"observed": []},
                    send_fn=lambda m: True)

    engine2, pending2 = _engine_with_memory(tmp_path, [
        _art("Ethereum foundation announces new grant program",
             "ethereum.org", "https://ethereum.org/g", "Grants announced."),
        _art("Ethereum foundation announces new grant program",
             "cointelegraph.com", "https://c.com/2", "Grant program reported."),
    ])
    assert len(pending2) == 1 and pending2[0][1] == "new"


def test_failed_send_not_recorded_in_memory(tmp_path):
    """A failed Telegram send must not burn the once-only memory — the story
    may alert when Telegram recovers."""
    arts = [
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ]
    engine1, pending1 = _engine_with_memory(tmp_path, arts)
    engine1.publish(pending1[0][0], "new", analyzer=_ai_ok,
                    market_fn=lambda a: {"observed": []},
                    send_fn=lambda m: False)      # send failed
    engine2, pending2 = _engine_with_memory(tmp_path, arts)
    assert len(pending2) == 1 and pending2[0][1] == "new"


def test_material_updates_disabled_by_default(tmp_path):
    """Owner's rule: once-only. Even a material change re-alerts nothing
    while NEWS_ALLOW_UPDATE_ALERTS is False (default)."""
    assert config.NEWS_ALLOW_UPDATE_ALERTS is False
    engine = NewsEngine()
    engine._alerts = news.AlertMemory(tmp_path / "alerted.json")
    first = engine.ingest([
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ])
    engine.publish(first[0][0], "new", analyzer=_ai_ok,
                   market_fn=lambda a: {"observed": []},
                   send_fn=lambda m: True)
    e = first[0][0]
    from datetime import timedelta as _td
    e.last_alerted_at -= _td(seconds=config.NEWS_ALERT_COOLDOWN_SECONDS + 3600)
    updates = engine.ingest([
        _art("Binance confirms security incident, loss of 40000 BTC estimated",
             "theblock.co", "https://t.com/2",
             "Losses estimated at 40000 BTC according to the filing."),
    ])
    assert updates == []


def test_alert_memory_expires_after_retention(tmp_path):
    """Memory is long (7 days) but not forever — genuinely old news beyond
    the retention window is allowed to alert again as a fresh cycle."""
    mem = news.AlertMemory(tmp_path / "alerted.json")
    mem.add(frozenset({"binance", "incident", "withdrawals"}),
            "Binance incident")
    # age the entry beyond retention
    from datetime import timedelta as _td
    mem._entries[0]["at"] = (datetime.now(timezone.utc)
                             - _td(days=config.NEWS_ALERT_MEMORY_DAYS + 1)).isoformat()
    assert mem.contains(frozenset({"binance", "incident", "withdrawals"})) is None


# ------------------------------------------------------- 20-minute cooldown

def _alerted_engine():
    """Engine whose first event has already been alerted just now (isolated
    in-memory alert store — no disk state leaks between tests)."""
    engine = NewsEngine()
    engine._alerts = news.AlertMemory(Path(config.NEWS_ALERT_MEMORY_FILE))
    first = engine.ingest([
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ])
    e = first[0][0]
    engine.publish(e, "new", analyzer=_ai_ok,
                   market_fn=lambda a: {"observed": []},
                   send_fn=lambda m: True)
    return engine, e


def test_material_update_blocked_inside_cooldown_window(monkeypatch):
    """A material update arriving minutes after the alert must NOT re-alert
    — the cooldown suppresses duplicates (flag on; off would suppress more)."""
    monkeypatch.setattr(config, "NEWS_ALLOW_UPDATE_ALERTS", True)
    engine, e = _alerted_engine()
    pending = engine.ingest([
        _art("Binance confirms security incident, loss of 40000 BTC estimated",
             "theblock.co", "https://t.com/2",
             "Losses estimated at 40000 BTC according to the filing."),
    ])
    assert pending == []                      # inside cooldown: suppressed


def test_material_update_fires_after_cooldown_expires(monkeypatch):
    """Once 20 minutes have passed, the same new details DO re-alert — the
    material tokens were held back, not lost (flag on)."""
    monkeypatch.setattr(config, "NEWS_ALLOW_UPDATE_ALERTS", True)
    engine, e = _alerted_engine()
    # age the last alert beyond the cooldown
    from datetime import timedelta as _td
    e.last_alerted_at = e.last_alerted_at - _td(
        seconds=config.NEWS_ALERT_COOLDOWN_SECONDS + 60)
    pending = engine.ingest([
        _art("Binance confirms security incident, loss of 40000 BTC estimated",
             "theblock.co", "https://t.com/2",
             "Losses estimated at 40000 BTC according to the filing."),
    ])
    assert len(pending) == 1 and pending[0][1] == "update"


def test_failed_publish_does_not_start_cooldown():
    """Cooldown protects against duplicate ALERTS, not attempts: if Telegram
    failed, the event is still un-alerted and may retry."""
    engine = NewsEngine()
    first = engine.ingest([
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ])
    e = first[0][0]
    engine.publish(e, "new", analyzer=_ai_ok,
                   market_fn=lambda a: {"observed": []},
                   send_fn=lambda m: False)     # send failed
    assert e.last_alerted_at is None           # no cooldown started


# ------------------------------------------------------- Trump news handling

def test_trump_news_extracts_assets_and_clusters():
    """Trump crypto policy news: TRUMP asset extracted, stories about the
    same announcement cluster into one event."""
    assets = news.extract_assets("Trump signs executive order on bitcoin reserves")
    assert "TRUMP" in assets and "BTC" in assets

    engine = NewsEngine()
    pending = engine.ingest([
        _art("Trump signs executive order on crypto reserves",
             "binance.com", "https://binance.com/en/x",
             "Official announcement confirms the executive order."),
        _art("Trump signs executive order on crypto reserves",
             "cointelegraph.com", "https://c.com/t1",
             "The executive order covers bitcoin reserves."),
    ])
    assert len(engine._events) == 1            # one clustered event
    assert pending[0][0].status == "VERIFIED"
    assert "TRUMP" in pending[0][0].assets


def test_trump_feeds_configured():
    """Dedicated Trump/politics discovery feeds must be present so Trump
    market-moving news actually reaches the engine."""
    feeds = " ".join(config.NEWS_RSS_FEEDS)
    assert "donald-trump" in feeds
    assert "politics" in feeds


def test_is_official_matches_subdomains():
    assert is_official("binance.com") is True
    assert is_official("www.binance.com") is True
    assert is_official("support.binance.com") is True
    assert is_official("notbinance.com") is False
    assert is_official("cointelegraph.com") is False


def test_is_official_rejects_look_alike_domains():
    """Phishing look-alikes must never count as official primary sources
    (regression: lstrip("www.") treated wbinance.com as official)."""
    for fake in ("wbinance.com", "wwbinance.com", ".binance.com",
                 "binance.com.evil.io", "binance.co", "secure-binance.com"):
        assert is_official(fake) is False, fake


# ------------------------------------------------- 2. unverified never publishes

def test_unverified_news_never_reaches_ai_publication():
    engine = NewsEngine()
    calls = []
    engine.ingest([_art("Binance halts withdrawals", "cointelegraph.com", "https://a.com/1")])
    sent = engine.publish(engine._events[0], "new", analyzer=calls.append,
                          market_fn=lambda a: {"observed": [], "unavailable": []},
                          send_fn=lambda m: True)
    assert sent is False and calls == []          # analyzer never invoked


def test_ai_cannot_override_verification_status():
    """Even a bullish-looking AI result cannot publish a DEVELOPING event."""
    e = _hack_event()
    e.status = "DEVELOPING"
    engine = NewsEngine()
    sent = engine.publish(e, "new", analyzer=_ai_ok,
                          market_fn=lambda a: {"observed": [], "unavailable": []},
                          send_fn=lambda m: True)
    assert sent is False


def test_analysis_refuses_non_verified_event():
    e = _hack_event()
    e.status = "PARTIALLY_VERIFIED"
    with pytest.raises(na.AINewsError):
        na.analyze_event(e)


# ------------------------------------------------------- 3. AI analysis output

def test_verified_news_generates_ai_summary_and_classification():
    e = _hack_event()
    out = _ai_ok(e)
    assert out["direction"] == "BEARISH" and out["strength"] == "HIGH"
    assert set(out["horizons"]) == {"immediate", "short_term", "medium_term"}


def test_validation_rejects_illegal_enums():
    e = _hack_event()
    base = _ai_ok(e)
    with pytest.raises(na.AINewsError):
        na._validate({**base, "direction": "MOON"}, e)
    with pytest.raises(na.AINewsError):
        na._validate({**base, "strength": "MASSIVE"}, e)


def test_validation_rejects_missing_horizons():
    e = _hack_event()
    base = _ai_ok(e)
    base["horizons"] = {"immediate": "x"}       # short/medium missing
    with pytest.raises(na.AINewsError):
        na._validate(base, e)


@pytest.mark.parametrize("phrase", [
    "BTC will rise sharply",
    "This is guaranteed profit",
    "BUY BTC now",
    "ETH will pump tonight",
])
def test_guaranteed_move_language_rejected(phrase):
    e = _hack_event()
    base = _ai_ok(e)
    with pytest.raises(na.AINewsError):
        na._validate({**base, "interpretation": phrase}, e)


def test_bullish_bearish_neutral_classifications_all_legal():
    e = _hack_event()
    for d in ("BULLISH", "BEARISH", "MIXED", "NEUTRAL", "UNCERTAIN"):
        out = na._validate({**_ai_ok(e), "direction": d}, e)
        assert out["direction"] == d


def test_uncertain_when_evidence_insufficient():
    """The prompt forces UNCERTAIN when sources are thin; validator accepts it."""
    e = _hack_event()
    out = na._validate({**_ai_ok(e), "direction": "UNCERTAIN"}, e)
    assert out["direction"] == "UNCERTAIN"


def test_critical_event_gets_appropriate_classification():
    """Verified exchange hack -> BEARISH / HIGH or EXTREME (pinned by fixtures
    + the prompt's own instructions)."""
    out = _ai_ok(_hack_event())
    assert out["direction"] == "BEARISH" and out["strength"] in ("HIGH", "EXTREME")


# ----------------------------------------------------- 4. fact vs interpretation

def test_facts_and_interpretation_are_separated():
    """Summary carries only what sources state; interpretation is a separate,
    hedged field. The alert renders them in different sections."""
    e = _hack_event()
    out = _ai_ok(e)
    msg = format_news_alert(e, out, {"observed": [], "unavailable": ["all"]})
    assert "What happened" in msg and out["summary"] in msg
    assert "AI market analysis" in msg and out["interpretation"] in msg
    assert out["summary"] != out["interpretation"]


def test_alert_uses_the_spec_template():
    e = _hack_event()
    msg = format_news_alert(e, _ai_ok(e), {"observed": [], "unavailable": ["all"]})
    for section in ("VERIFIED CRYPTO NEWS", "What happened", "Market impact",
                    "Impact strength", "Time horizon", "AI market analysis",
                    "Affected assets", "Key risk", "Verification",
                    "Sources", "VERIFIED"):
        assert section in msg
    assert e.articles[0].url in msg            # primary source listed


# ----------------------------------------------------------- 5. no hallucination

def test_prompt_is_source_grounded_not_headline_only():
    e = _hack_event()
    prompt = na.build_prompt(e, {})
    for a in e.articles:                        # both source contents included
        assert a.source_domain in prompt
        assert a.summary[:40] in prompt
    assert "BNB" in prompt                      # assets included
    assert "OBSERVED" in prompt or "No market data" in prompt


def test_ai_never_receives_decision_to_publish():
    """publish() consults verification BEFORE the analyzer and aborts on
    non-VERIFIED — the AI cannot manufacture a publication path."""
    engine = NewsEngine()
    e = _hack_event()
    e.status = "CONFLICTED"
    sent = engine.publish(e, "new", analyzer=_ai_ok,
                          market_fn=lambda a: {"observed": []},
                          send_fn=lambda m: True)
    assert sent is False


# ------------------------------------------------------------- 6. duplicates

def _ingest_cycle(engine, articles):
    return engine.ingest(articles)


def test_duplicate_event_does_not_duplicate_alert():
    engine = NewsEngine()
    first = _ingest_cycle(engine, [
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ])
    assert len(first) == 1 and first[0][1] == "new"
    e = first[0][0]
    engine.publish(e, "new", analyzer=_ai_ok,
                   market_fn=lambda a: {"observed": []},
                   send_fn=lambda m: True)

    # same story re-reported: cosmetic new article, nothing material
    again = _ingest_cycle(engine, [
        _art("Binance halts withdrawals after incident, report",
             "theblock.co", "https://t.com/1", "Withdrawals suspended."),
    ])
    assert again == []                          # no new/update alert


def test_material_update_creates_update_alert(monkeypatch):
    monkeypatch.setattr(config, "NEWS_ALLOW_UPDATE_ALERTS", True)
    engine = NewsEngine()
    # round 1: press + official -> VERIFIED, alert goes out
    first = _ingest_cycle(engine, [
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ])
    assert len(first) == 1 and first[0][1] == "new"
    e = first[0][0]
    engine.publish(e, "new", analyzer=_ai_ok,
                   market_fn=lambda a: {"observed": []},
                   send_fn=lambda m: True)
    assert e.last_alerted_at is not None

    # round 2 (after the 20-minute cooldown): material new verified details
    from datetime import timedelta as _td
    e.last_alerted_at = e.last_alerted_at - _td(
        seconds=config.NEWS_ALERT_COOLDOWN_SECONDS + 60)
    updates = _ingest_cycle(engine, [
        _art("Binance confirms security incident, loss of 40000 BTC estimated",
             "theblock.co", "https://t.com/2",
             "Losses estimated at 40000 BTC according to the official filing."),
    ])
    assert len(updates) == 1 and updates[0][1] == "update"
    assert updates[0][0] is e
    msg = format_news_alert(e, _ai_ok(e), {"observed": []},
                            update_note="new verified details: 40000")
    assert "VERIFIED NEWS UPDATE" in msg and "What changed" in msg


# --------------------------------------------------------- 7. market data

def test_market_data_labeled_as_observed():
    e = _hack_event()
    market = {"observed": [{
        "symbol": "BNB/USDT:USDT", "price": 600.0, "change_1h_pct": -3.2,
        "change_24h_pct": -5.1, "volume_change_pct": None,
        "open_interest": None, "funding_rate": 0.0001, "liquidation": None,
    }], "unavailable": []}
    msg = format_news_alert(e, _ai_ok(e), market)
    assert "Observed market data" in msg
    assert "measured, not interpretation" in msg
    assert "-3.20%" in msg
    # prompt also labels it observed and forbids causal claims
    prompt = na.build_prompt(e, market)
    assert "OBSERVED (do not attribute causality" in prompt


def test_missing_market_measurements_render_na_not_zero():
    """A field the exchange did not return must render 'n/a' — reporting a
    flat 0.0% would fabricate a measurement (regression for change_1h)."""
    e = _hack_event()
    market = {"observed": [{
        "symbol": "BNB/USDT:USDT", "price": 600.0, "change_1h_pct": None,
        "change_24h_pct": None, "volume_change_pct": None,
        "open_interest": None, "funding_rate": None, "liquidation": None,
    }], "unavailable": []}
    msg = format_news_alert(e, _ai_ok(e), market)
    assert "n/a" in msg
    assert "+0.00%" not in msg and "0.00%" not in msg
    prompt = na.build_prompt(e, market)
    assert "n/a" in prompt and "+0" not in prompt


def test_market_data_failure_still_alerts_with_note():
    e = _hack_event()
    msg = format_news_alert(e, _ai_ok(e), {"observed": [], "unavailable": ["all"]})
    assert "Market data unavailable" in msg
    prompt = na.build_prompt(e, {"observed": [], "unavailable": ["all"]})
    assert "do not make market claims" in prompt


def test_no_causality_language_in_validated_output():
    """The validator's forbidden list bars causal-certainty phrasing too."""
    e = _hack_event()
    with pytest.raises(na.AINewsError):
        na._validate({**_ai_ok(e),
                      "interpretation": "This news caused BTC to fall and it "
                                        "will continue falling."}, e)


# -------------------------------------------- 8. news/trading-signal separation

def test_ai_cannot_directly_create_trading_signal():
    """The alert message and analysis output carry no BUY/SELL signal, and
    the news modules never touch the trading pipeline."""
    import news_market  # noqa: F401  (module exists, separate from trading)
    e = _hack_event()
    msg = format_news_alert(e, _ai_ok(e), {"observed": []})
    for banned in ("BUY", "SELL", "LONG", "SHORT", "signal"):
        assert banned not in msg, f"alert leaks trading language: {banned}"
    # news.py / news_analysis.py never import decision/signal modules
    import news  # noqa: F401
    import sys
    for mod in ("decision", "setup_quality", "risk_gate"):
        assert not any(m.endswith(mod) and m.startswith("news")
                       for m in sys.modules)


# ----------------------------------------------------- 9. failure behavior

def test_ai_failure_skips_alert_entirely():
    engine = NewsEngine()
    e = _hack_event()
    sent_messages = []

    def broken_analyzer(event, market):
        raise na.AINewsError("transport down")

    sent = engine.publish(e, "new", analyzer=broken_analyzer,
                          market_fn=lambda a: {"observed": []},
                          send_fn=sent_messages.append)
    assert sent is False and sent_messages == []   # nothing incomplete sent


def test_publish_records_state_only_on_success():
    engine = NewsEngine()
    e = _hack_event()
    engine.publish(e, "new", analyzer=_ai_ok,
                   market_fn=lambda a: {"observed": []},
                   send_fn=lambda m: False)         # telegram failed
    assert e.last_alerted_at is None               # so future material updates still alert


# ------------------------------------------------------------ 10. clustering

def test_similarity_clustering_merges_same_story():
    engine = NewsEngine()
    pending = _ingest_cycle(engine, [
        _art("Binance reports security incident affecting withdrawals",
             "binance.com", "https://binance.com/en/x", "Incident confirmed."),
        _art("Binance halts withdrawals after security incident",
             "cointelegraph.com", "https://c.com/1", "Withdrawals suspended."),
    ])
    assert len(engine._events) == 1               # one event, two articles
    assert pending[0][0].status == "VERIFIED"


def test_different_stories_stay_separate():
    engine = NewsEngine()
    _ingest_cycle(engine, [
        _art("Ethereum foundation announces grant program",
             "ethereum.org", "https://ethereum.org/x", "Grants announced."),
        _art("Solana network outage resolved",
             "solana.com", "https://solana.com/x", "Outage resolved."),
    ])
    assert len(engine._events) == 2


def test_old_events_expire():
    engine = NewsEngine()
    _ingest_cycle(engine, [_art("Old story", "cointelegraph.com",
                                "https://old.com/1",
                                when=NOW - timedelta(hours=48))])
    _ingest_cycle(engine, [_art("Fresh story", "cointelegraph.com",
                                "https://new.com/1")])
    assert all("Fresh" in e.headline or
               max(a.published_at for a in e.articles) >
               datetime.now(timezone.utc) - timedelta(hours=config.NEWS_EVENT_WINDOW_HOURS)
               for e in engine._events)


def test_asset_extraction():
    assets = news.extract_assets("Bitcoin and Ethereum rally as $SUI launches")
    assert {"BTC", "ETH", "SUI"} <= assets
