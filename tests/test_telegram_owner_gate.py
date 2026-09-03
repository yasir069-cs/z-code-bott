"""Owner gate for the control commands — the bot must fail CLOSED.

/scan_on starts a 24/7 scan session and /scan_off stops it. They used to be
guarded by "if TELEGRAM_CHAT_ID is unset, everyone is the owner", which let any
stranger who found the bot drive the exchange rate budget and burn the AI daily
cap. There is no safe way to infer an owner from an empty allow-list, so an
empty allow-list authorises nobody.
"""
import asyncio

import pytest

import config
import telegram_bot


class _Chat:
    def __init__(self, chat_id):
        self.id = chat_id


class _Message:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)


class _Update:
    def __init__(self, chat_id=None):
        self.effective_chat = _Chat(chat_id) if chat_id is not None else None
        self.message = _Message()


@pytest.fixture(autouse=True)
def _no_owner(monkeypatch):
    """Default: nothing configured, so nothing is authorised."""
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")


# ─────────────────────────────────────────────────────────── allow-list parsing

def test_owner_ids_are_a_comma_separated_allow_list():
    config.TELEGRAM_CHAT_ID = "111, 222 ,"
    assert telegram_bot.owner_chat_ids() == {"111", "222"}


def test_no_configuration_means_no_owner():
    assert telegram_bot.owner_chat_ids() == set()


def test_owner_check_requires_a_matching_chat_id():
    config.TELEGRAM_CHAT_ID = "111,222"
    assert telegram_bot._is_owner(_Update(222)) is True
    assert telegram_bot._is_owner(_Update(333)) is False
    # a message with no chat at all is not an owner either
    assert telegram_bot._is_owner(_Update(None)) is False


# ────────────────────────────────────────────────────────────── fail-closed ──

def test_unconfigured_bot_refuses_everyone(monkeypatch):
    """The regression: unset TELEGRAM_CHAT_ID used to authorise the whole world.

    Both command effects are stubbed, so a future slip in the guard fails an
    assertion here instead of really starting a scan session against Binance."""
    called = []
    import main
    monkeypatch.setattr(telegram_bot.ondemand, "stop_ondemand_scan",
                        lambda: called.append("stop") or {"status": "not_running"})
    monkeypatch.setattr(main, "start_ondemand_scan",
                        lambda: called.append("start") or {"status": "started"})
    update = _Update(999)                          # some random stranger
    assert telegram_bot._is_owner(update) is False
    asyncio.run(telegram_bot.cmd_scan_off(update, None))
    assert called == []
    assert "TELEGRAM_CHAT_ID is not set" in update.message.sent[0]


def test_denial_explains_the_missing_configuration(monkeypatch):
    """Denied with a reason the owner can act on — and nothing is started."""
    started = []
    import main
    monkeypatch.setattr(main, "start_ondemand_scan",
                        lambda: started.append(1) or {"status": "started"})
    update = _Update(111)
    asyncio.run(telegram_bot.cmd_scan_on(update, None))
    assert started == []
    assert update.message.sent
    assert "TELEGRAM_CHAT_ID is not set" in update.message.sent[0]


def test_scan_on_is_not_started_for_a_non_owner(monkeypatch):
    """A denied /scan_on must not reach main's on-demand starter at all."""
    config.TELEGRAM_CHAT_ID = "111"
    started = []
    import main
    monkeypatch.setattr(main, "start_ondemand_scan",
                        lambda: started.append(1) or {"status": "started"})
    stranger = _Update(222)
    asyncio.run(telegram_bot.cmd_scan_on(stranger, None))
    assert started == []
    assert "Access denied" in stranger.message.sent[0]

    owner = _Update(111)
    asyncio.run(telegram_bot.cmd_scan_on(owner, None))
    # the real start happens on a worker thread; the guard must have let it go
    assert owner.message.sent and "Starting on-demand scan" in owner.message.sent[0]
