"""ScanCoordinator + AI opinion worker tests (audit-row integrity)."""
import csv
import threading
import time

import config
import scan_coordinator


def _records():
    return [
        {"symbol": "A/USDT:USDT", "signal_id": "s-A", "deterministic_decision": "LONG"},
        {"symbol": "B/USDT:USDT", "signal_id": "s-B", "deterministic_decision": "NO_TRADE"},
    ]


def _bundles():
    return [{"symbol": "A/USDT:USDT"}, {"symbol": "B/USDT:USDT"}]


def test_unanswered_verdicts_are_labelled_unavailable(tmp_path, monkeypatch):
    """A record with no LLM answer must read UNAVAILABLE in ai_opinions.csv —
    the old `status if verdict else status` no-op mislabeled it SUCCESS with
    an empty opinion, corrupting the audit trail."""
    path = tmp_path / "ai_opinions.csv"
    monkeypatch.setattr(config, "AI_OPINIONS_LOG_FILE", path)

    def verdicts(bundles):
        # only A gets an answer; B stays unanswered
        return {"A/USDT:USDT": {"signal": "LONG", "confidence": 70,
                                "reason": "ok", "ai_used": True}}

    worker = scan_coordinator.AIOpinionWorker(verdicts)
    worker._run("scan-1", _records(), _bundles())

    with path.open(newline="", encoding="utf-8") as fh:
        rows = {r["symbol"]: r for r in csv.DictReader(fh)}
    assert rows["A/USDT:USDT"]["ai_status"] == "SUCCESS"
    assert rows["A/USDT:USDT"]["ai_opinion"] == "LONG"
    assert rows["B/USDT:USDT"]["ai_status"] == "UNAVAILABLE"
    assert rows["B/USDT:USDT"]["ai_opinion"] == ""


def test_transport_failure_labels_every_record_failed(tmp_path, monkeypatch):
    path = tmp_path / "ai_opinions.csv"
    monkeypatch.setattr(config, "AI_OPINIONS_LOG_FILE", path)

    def boom(bundles):
        raise RuntimeError("transport down")

    worker = scan_coordinator.AIOpinionWorker(boom)
    try:
        worker._run("scan-2", _records(), _bundles())
        raise AssertionError("should re-raise")
    except RuntimeError:
        pass
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert all(r["ai_status"] == "FAILED" for r in rows)
    assert "transport down" in rows[0]["ai_reason"]


def test_submit_never_blocks_when_queue_full(monkeypatch, tmp_path):
    """A full queue drops the batch (returns False) instead of blocking the
    scan. The worker is pinned with an Event so the queue state is
    deterministic: task 1 running, task 2 queued, task 3 must be dropped."""
    monkeypatch.setattr(config, "AI_OPINIONS_LOG_FILE",
                        tmp_path / "ai_opinions.csv")
    monkeypatch.setattr(config, "AI_WORKER_MAX_PENDING", 1)
    release = threading.Event()

    def slow_verdicts(bundles):
        release.wait(5)          # block the single worker until released
        return {}

    worker = scan_coordinator.AIOpinionWorker(slow_verdicts)
    try:
        assert worker.submit("s1", _records(), _bundles()) is True
        # wait for the worker to START task 1 (pending decremented on start)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and worker._pending > 0:
            time.sleep(0.01)
        assert worker.submit("s2", _records(), _bundles()) is True   # queued
        assert worker.submit("s3", _records(), _bundles()) is False  # dropped
        assert worker.last_status == "UNAVAILABLE"
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and worker._pending > 0:
            time.sleep(0.01)     # let the queued task start & drain
