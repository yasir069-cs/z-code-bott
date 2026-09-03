"""Tunable floors (config `_env_number` / `_env_flag`).

The 2026-09-02 incident is the reason these exist: floors were loosened 10-35% in
`config.py` during a live session, the change was committed, and it never produced a
signal — the reward side was measured wrongly, not gated too tightly — while the repo
was left holding numbers that no test or doc agreed with. So the numbers are now
overridable from `.env` (an experiment needs no code edit), the DEFAULTS are pinned
here against the spec, and an override that empties a rule announces itself at
startup instead of looking like a silent regression.
"""
import importlib

import os

import pytest

import config


_OVERRIDABLE = ("MIN_RR", "QUALITY_MIN", "QUALITY_PRIMARY_FLOOR", "ALERT_QUALITY_MIN",
                "ALERT_TIER_NORMAL_MIN", "ALERT_TIER_HIGH_MIN", "ALERT_TIER_STRONG_MIN",
                "RISK_TARGET_SCAN_ZONES", "AI_JSON_MODE", "MIN_SCORE_1H", "MIN_SCORE_15M",
                "MIN_SCORE_5M", "MIN_CONFLUENCE", "RISK_MAX_STOP_ATR", "RISK_MIN_TARGET_ATR")


@pytest.fixture
def reloaded():
    """Reload config with a set of env overrides, then restore the shipped defaults.

    Reloading mutates the module object every other module already imported, so the
    teardown reload is what keeps the rest of the suite on real defaults — without it
    a leaked `MIN_RR=0.1` would silently change unrelated tests.
    """
    def _reload(**overrides):
        for key in _OVERRIDABLE:
            os.environ.pop(key, None)
        os.environ.update({k: str(v) for k, v in overrides.items()})
        return importlib.reload(config)

    yield _reload

    for key in _OVERRIDABLE:
        os.environ.pop(key, None)
    importlib.reload(config)


# ------------------------------------------------------------- the shipped numbers

def test_defaults_are_the_spec_values_not_the_incident_ones():
    """If a merge ever re-imports the loosened config, this test fails on the spot."""
    assert config.QUALITY_PRIMARY_FLOOR == 45.0
    assert config.QUALITY_MIN == 50.0
    assert config.ALERT_QUALITY_MIN == 50.0
    assert (config.ALERT_TIER_NORMAL_MIN, config.ALERT_TIER_HIGH_MIN,
            config.ALERT_TIER_STRONG_MIN) == (50.0, 60.0, 70.0)
    assert config.MIN_RR == 1.5
    assert (config.MIN_SCORE_1H, config.MIN_SCORE_15M, config.MIN_SCORE_5M,
            config.MIN_CONFLUENCE) == (55.0, 50.0, 50.0, 55.0)
    # main's commit set both to 0.0 while also disabling the `poor_rr` gate
    assert config.QUALITY_RR_NONE_PENALTY == 30.0
    assert config.QUALITY_RR_MISS_PENALTY == 20.0
    # indicators stay secondary: a small confirm bonus, a bigger conflict penalty
    assert config.IND_CONFIRM_BONUS_MAX == 10.0
    assert config.IND_CONFLICT_PENALTY_MAX == 15.0


def test_batch_sizing_from_main_is_kept():
    """8000/90s was main's genuine improvement (a 20-coin batch does not fit in 2000
    tokens) — the merge takes it, and the escalation cap still sits above it."""
    assert config.AI_MAX_TOKENS == 8000
    assert config.AI_TIMEOUT_SECONDS == 90.0
    assert config.AI_MAX_TOKENS_RETRY_CAP > config.AI_MAX_TOKENS


# ---------------------------------------------------------------- the overrides

def test_an_env_override_changes_the_floor(reloaded):
    c = reloaded(MIN_RR="1.2", QUALITY_MIN="35", ALERT_QUALITY_MIN="30")
    assert c.MIN_RR == 1.2 and c.QUALITY_MIN == 35.0 and c.ALERT_QUALITY_MIN == 30.0


def test_a_garbage_override_is_fatal_rather_than_silently_ignored(reloaded):
    """"We set MIN_RR=1.2 and nothing changed" is only ever a silent-ignore bug.
    Fail at import, with the name and the value in the message."""
    with pytest.raises(RuntimeError) as exc:
        reloaded(MIN_RR="one-point-two")
    assert "MIN_RR" in str(exc.value) and "one-point-two" in str(exc.value)


def test_flags_accept_the_usual_spellings(reloaded):
    assert reloaded(RISK_TARGET_SCAN_ZONES="off").RISK_TARGET_SCAN_ZONES is False
    assert reloaded(RISK_TARGET_SCAN_ZONES="0").RISK_TARGET_SCAN_ZONES is False
    assert reloaded(RISK_TARGET_SCAN_ZONES="true").RISK_TARGET_SCAN_ZONES is True
    assert reloaded(AI_JSON_MODE="no").AI_JSON_MODE is False
    with pytest.raises(RuntimeError):
        reloaded(RISK_TARGET_SCAN_ZONES="maybe")


# ------------------------------------------------------------------- validation

def test_defaults_produce_no_floor_warnings():
    warnings = config.check_config_warnings()
    assert not [w for w in warnings if "band" in w or "enforced NOWHERE" in w
                or "above QUALITY_MIN" in w]


def test_an_out_of_band_floor_warns(monkeypatch):
    monkeypatch.setattr(config, "QUALITY_PRIMARY_FLOOR", 25.0)   # main's value
    monkeypatch.setattr(config, "QUALITY_MIN", 35.0)
    monkeypatch.setattr(config, "ALERT_QUALITY_MIN", 30.0)
    monkeypatch.setattr(config, "MIN_RR", 1.2)
    text = " ".join(config.check_config_warnings())
    assert "QUALITY_PRIMARY_FLOOR=25" in text
    assert "ALERT_QUALITY_MIN (30)" in text or "QUALITY_MIN" in text


def test_rr_enforced_nowhere_is_named_explicitly(monkeypatch):
    """The exact state `main` shipped: both quality RR penalties zeroed AND the RR
    gate disabled. A general "value out of band" line would not have said what is
    wrong, and the symptom (signals with no measurable reward) is unfalsifiable."""
    monkeypatch.setattr(config, "QUALITY_RR_NONE_PENALTY", 0.0)
    monkeypatch.setattr(config, "QUALITY_RR_MISS_PENALTY", 0.0)
    monkeypatch.setattr(config, "MIN_RR", 0.0)
    warnings = config.check_config_warnings()
    assert any("reward:risk is enforced NOWHERE" in w for w in warnings)


def test_alert_floor_above_decision_floor_is_pointed_out(monkeypatch):
    monkeypatch.setattr(config, "ALERT_QUALITY_MIN", 70.0)
    monkeypatch.setattr(config, "QUALITY_MIN", 50.0)
    warnings = config.check_config_warnings()
    assert any("above QUALITY_MIN" in w for w in warnings)


def test_no_hand_rolled_floor_parsing_left_behind():
    """Both helpers are the only path from .env to a floor — an inline
    `float(os.getenv(...))` would silently skip the validation that makes the
    override visible at startup."""
    import pathlib
    text = pathlib.Path("config.py").read_text(encoding="utf-8")
    inline = [line for line in text.splitlines()
              if line.startswith(("MIN_SCORE", "QUALITY_", "ALERT_", "MIN_RR",
                                  "MIN_CONFLUENCE", "RISK_MAX", "RISK_MIN"))
              and "os.getenv" in line and "_env_" not in line]
    assert not inline
