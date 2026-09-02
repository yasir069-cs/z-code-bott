"""Exposed-credential detection: the bot must warn on every startup while a
known-leaked key is still configured, and stay silent once rotated.

These fixtures use SYNTHETIC values built from the marker prefixes in config.
The full live token used to be pasted here — which is how a real secret got
committed to a public repo — so a test below asserts none of these files carry
one again."""
import pathlib

import pytest

import config


def test_exposed_telegram_token_warns(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN",
                        config._EXPOSED_TELEGRAM_TOKENS[0] + "-SYNTHETIC-NOT-A-REAL-TOKEN")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-cleankey")
    warnings = config.check_exposed_credentials()
    assert len(warnings) == 1 and "TELEGRAM_TOKEN" in warnings[0]


def test_exposed_openrouter_key_warns(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123:clean")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY",
                        config._EXPOSED_OPENROUTER_KEYS[0] + "6fd4159313faaf968292e38a")
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


def test_the_key_pasted_into_chat_on_2026_09_02_is_flagged(monkeypatch):
    """Rotation is only half the fix: until it happens, the boot must keep saying so."""
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "123:clean")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-87531e9388a2a465anything")
    warnings = config.check_exposed_credentials()
    assert len(warnings) == 1 and "OPENROUTER_API_KEY" in warnings[0]


def test_the_repo_stores_prefixes_never_full_secrets():
    """A leak-watchdog written as a full secret is itself the leak."""
    for group in (config._EXPOSED_TELEGRAM_TOKENS, config._EXPOSED_OPENROUTER_KEYS):
        for marker in group:
            assert len(marker) <= 25, marker          # prefix, not the credential
            assert marker.count("-") < 4              # a token body has more segments


def test_no_committed_file_carries_a_live_token_or_key():
    """The regression: a test fixture once held the bot's real token verbatim."""
    import re
    import subprocess
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout.split()
    secret = re.compile(r"(sk-or-v1-[0-9a-f]{32,}|\d{8,12}:AA[A-Za-z0-9_-]{30,})")
    offenders = []
    for f in files:
        try:
            text = pathlib.Path(f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if secret.search(text):
            offenders.append(f)
    assert offenders == [], f"full credentials committed in: {offenders}"


# ── .env sanity: a malformed AI_BASE_URL must be loud, not silent ────────────

def test_markdown_pasted_base_url_is_detected(monkeypatch):
    """`AI_BASE_URL=[https://host/v1](https://host/v1)` — a link copied out of a
    chat window — is what made every AI call fail while the budget kept burning."""
    monkeypatch.setattr(config, "AI_BASE_URL",
                        "[https://openrouter.ai/api/v1](https://openrouter.ai/api/v1)")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-whatever")
    warnings = config.check_config_warnings()
    assert len(warnings) == 1 and "markdown link" in warnings[0]


@pytest.mark.parametrize("url", [
    "https://openrouter.ai/api/v1",
    "https://agentrouter.org/v1/",
    "http://localhost:1234/v1",          # local proxy is a legitimate value
])
def test_valid_base_urls_are_accepted(monkeypatch, url):
    monkeypatch.setattr(config, "AI_BASE_URL", url)
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-whatever")
    monkeypatch.setattr(config, "AI_MODEL", "some-model")
    monkeypatch.setattr(config, "AI_MODEL_FALLBACK", "")
    assert config.check_config_warnings() == []


def test_quoted_and_empty_values_are_reported(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-whatever")
    monkeypatch.setattr(config, "AI_BASE_URL", '"https://host/v1"')
    assert any("not a usable URL" in w for w in config.check_config_warnings())
    monkeypatch.setattr(config, "AI_BASE_URL", "https://host/v1")
    monkeypatch.setattr(config, "AI_MODEL", "")
    assert any("AI_MODEL is empty" in w for w in config.check_config_warnings())


def test_duplicate_fallback_model_is_reported(monkeypatch):
    """The ladder would just hit the same model twice, burning 2x the budget."""
    monkeypatch.setattr(config, "AI_BASE_URL", "https://host/v1")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-whatever")
    monkeypatch.setattr(config, "AI_MODEL", "deepseek-v4-flash")
    monkeypatch.setattr(config, "AI_MODEL_FALLBACK", "deepseek-v4-flash")
    assert any("equals AI_MODEL" in w for w in config.check_config_warnings())
