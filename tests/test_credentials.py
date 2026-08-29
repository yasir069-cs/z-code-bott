"""Exposed-credential detection: the bot must warn on every startup while a
known-leaked key is still configured, and stay silent once rotated."""
import config


def test_exposed_telegram_token_warns(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN",
                        "8851597372:AAFlynes-zp14tnQLHwcSBC34iCy62J8N_Q")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-cleankey")
    warnings = config.check_exposed_credentials()
    assert len(warnings) == 1 and "TELEGRAM_TOKEN" in warnings[0]


def test_exposed_openrouter_key_warns(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123:clean")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY",
                        "sk-or-v1-5e4bb826af4b6fd4159313faaf968292e38a")
    warnings = config.check_exposed_credentials()
    assert len(warnings) == 1 and "OPENROUTER_API_KEY" in warnings[0]


def test_clean_credentials_silent(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "999:rotated-token")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-rotated")
    assert config.check_exposed_credentials() == []


def test_empty_credentials_silent(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    assert config.check_exposed_credentials() == []


def test_official_announcement_feeds_configured():
    """Ethereum Foundation blog + Solana news are official primary sources —
    an announcement there plus one news report verifies an event."""
    feeds = " ".join(config.NEWS_RSS_FEEDS)
    assert "blog.ethereum.org" in feeds
    assert "solana.com/news" in feeds
