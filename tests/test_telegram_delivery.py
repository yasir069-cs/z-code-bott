import asyncio

import alerts


class _FakeBot:
    sent = []
    failures = set()

    def __init__(self, token):
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send_message(self, *, chat_id, **kwargs):
        if chat_id in self.failures:
            raise RuntimeError(f"failed {chat_id}")
        self.sent.append((chat_id, kwargs["text"]))


def test_telegram_chat_ids_split_and_trim(monkeypatch):
    monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", " 111 , ,222,111 ")
    assert alerts.telegram_chat_ids() == ["111", "222", "111"]


def test_one_shot_delivery_sends_each_recipient(monkeypatch):
    _FakeBot.sent = []
    _FakeBot.failures = set()
    monkeypatch.setattr(alerts.config, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", "111,222")
    monkeypatch.setattr(alerts.telegram, "Bot", _FakeBot)

    asyncio.run(alerts._send("hello"))

    assert [chat_id for chat_id, _ in _FakeBot.sent] == ["111", "222"]


def test_one_shot_delivery_keeps_partial_success(monkeypatch):
    _FakeBot.sent = []
    _FakeBot.failures = {"111"}
    monkeypatch.setattr(alerts.config, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", "111,222")
    monkeypatch.setattr(alerts.telegram, "Bot", _FakeBot)

    asyncio.run(alerts._send("hello"))

    assert [chat_id for chat_id, _ in _FakeBot.sent] == ["222"]


def test_send_text_rejects_empty_recipient_list(monkeypatch):
    monkeypatch.setattr(alerts.config, "TELEGRAM_TOKEN", "token")
    monkeypatch.setattr(alerts.config, "TELEGRAM_CHAT_ID", ", ,")
    assert alerts.send_telegram_text("hello") is False
