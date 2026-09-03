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


# ── audit honesty: agreement column, header drift, budget notice ────────────

def _worker_with(verdicts, tmp_path, monkeypatch, **kw):
    path = tmp_path / "ai_opinions.csv"
    monkeypatch.setattr(config, "AI_OPINIONS_LOG_FILE", path)
    return scan_coordinator.AIOpinionWorker(verdicts, **kw), path


def test_agreement_column_grades_opinion_vs_emitted_verdict(tmp_path, monkeypatch):
    """The audit file must show disagreement, not just restate the Python call.

    `final_decision` stays the deterministic verdict (the stage is audit-only),
    while `agreement` says what the model thought of it."""
    def verdicts(bundles):
        return {"A/USDT:USDT": {"signal": "NO_TRADE", "confidence": 40, "reason": "thin"},
                "B/USDT:USDT": {"signal": "SHORT", "confidence": 66, "reason": "sweep"}}
    worker, path = _worker_with(verdicts, tmp_path, monkeypatch)
    worker._run("scan-3", _records(), _bundles())

    with path.open(newline="", encoding="utf-8") as fh:
        rows = {r["symbol"]: r for r in csv.DictReader(fh)}
    assert rows["A/USDT:USDT"]["agreement"] == "VETO_PROPOSED"     # LONG -> model: no trade
    assert rows["A/USDT:USDT"]["final_decision"] == "LONG"          # what actually shipped
    assert rows["B/USDT:USDT"]["agreement"] == "SIGNAL_PROPOSED"   # HOLD -> model wants SHORT


def test_agreement_labels_agree_and_no_answer(tmp_path, monkeypatch):
    def verdicts(bundles):
        return {"A/USDT:USDT": {"signal": "LONG", "confidence": 80, "reason": "aligned"}}
    worker, path = _worker_with(verdicts, tmp_path, monkeypatch)
    worker._run("scan-4", _records(), _bundles())
    with path.open(newline="", encoding="utf-8") as fh:
        rows = {r["symbol"]: r for r in csv.DictReader(fh)}
    assert rows["A/USDT:USDT"]["agreement"] == "AGREE"
    assert rows["B/USDT:USDT"]["agreement"] == "NO_ANSWER"


def test_stale_header_is_archived_not_written_over(tmp_path, monkeypatch):
    """A file written before `agreement` existed must be archived, exactly like
    signals_log.csv — never patched in place, never shifted columns."""
    old_header = ["timestamp", "scan_id", "signal_id", "symbol",
                  "deterministic_decision", "ai_opinion", "ai_status",
                  "ai_confidence", "ai_reason", "final_decision"]
    path = tmp_path / "ai_opinions.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(old_header)
        w.writerow(["2026-09-01T18:00:00+05:30", "old", "s", "A/USDT:USDT",
                    "LONG", "LONG", "SUCCESS", 70, "was", "LONG"])
    monkeypatch.setattr(config, "AI_OPINIONS_LOG_FILE", path)

    worker = scan_coordinator.AIOpinionWorker(lambda b: {})
    worker._run("scan-5", _records(), _bundles())

    assert (tmp_path / "ai_opinions.csv.v1.bak").exists()
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2 and rows[0]["agreement"] == "NO_ANSWER"
    with (tmp_path / "ai_opinions.csv.v1.bak").open() as fh:
        assert "was" in fh.read()                 # old history preserved


def test_budget_exhaustion_notifies_once(tmp_path, monkeypatch):
    """The documented one-time Telegram notice was never wired to a caller."""
    sent = []
    notices = iter(["AI budget exhausted for 2026-09-02 (50/50 requests).", None])
    worker = scan_coordinator.AIOpinionWorker(
        lambda b: {}, notify=sent.append, budget_notice_fn=lambda: next(notices))
    worker._notify_budget_exhausted()
    worker._notify_budget_exhausted()             # second call: nothing to say
    assert len(sent) == 1 and "exhausted" in sent[0]


def test_worker_survives_a_broken_notify(tmp_path, monkeypatch):
    def explode(text):
        raise RuntimeError("telegram down")
    worker = scan_coordinator.AIOpinionWorker(
        lambda b: {}, notify=explode,
        budget_notice_fn=lambda: "AI budget exhausted")
    worker._notify_budget_exhausted()              # must not raise
