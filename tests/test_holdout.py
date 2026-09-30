"""The holdout guard must fail loudly, and the frozen protocol must stay sane."""

from __future__ import annotations

from datetime import date

import pytest

from bnfmm.analysis.holdout import (
    PROTOCOL_PATH,
    HoldoutLocked,
    ProtocolError,
    count_configs,
    load_protocol,
    log_config,
    require_access,
)

PROTO = """
develop: [2026-08-24, 2026-08-25]
holdout: [2026-08-31]
excluded: {2026-08-26: "broken"}
expiry: {2026-08-24: 2026-08-25, 2026-08-25: 2026-08-25, default: 2026-09-29}
inclusion: {max_gap_s: 30, min_depth_coverage: 0.25, min_feed_coverage: 0.25}
baseline: {half_spread_ticks: 6}
"""


@pytest.fixture
def proto(tmp_path):
    p = tmp_path / "protocol.yaml"
    p.write_text(PROTO)
    return load_protocol(p)


def test_develop_days_need_no_unlock(proto, tmp_path):
    log = tmp_path / "h.md"
    assert require_access(proto, [date(2026, 8, 24)], log_path=log) == [date(2026, 8, 24)]
    assert not log.exists()


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_holdout_refused_without_a_reason(proto, tmp_path, reason):
    log = tmp_path / "h.md"
    with pytest.raises(HoldoutLocked):
        require_access(proto, [date(2026, 8, 24), date(2026, 8, 31)],
                       unlock_reason=reason, log_path=log)
    assert not log.exists()


def test_unlock_is_logged_with_hash_and_reason(proto, tmp_path):
    log = tmp_path / "h.md"
    require_access(proto, [date(2026, 8, 31)], unlock_reason="baseline | first look",
                   log_path=log, argv=["backtest.py"])
    require_access(proto, [date(2026, 8, 31)], unlock_reason="second", log_path=log, argv=["x"])
    text = log.read_text()
    assert text.count("2026-08-31") == 2
    assert proto.sha256[:12] in text
    assert "baseline / first look" in text  # a pipe would break the table


def test_excluded_and_unknown_days_raise(proto, tmp_path):
    with pytest.raises(ProtocolError, match="excluded"):
        require_access(proto, [date(2026, 8, 26)], log_path=tmp_path / "h.md")
    with pytest.raises(ProtocolError, match="neither"):
        require_access(proto, [date(2026, 8, 27)], log_path=tmp_path / "h.md")


def test_expiry_lookup(proto):
    assert proto.expiry_for(date(2026, 8, 24)) == date(2026, 8, 25)
    assert proto.expiry_for(date(2026, 8, 31)) == date(2026, 9, 29)


@pytest.mark.parametrize("bad", [
    PROTO.replace("holdout: [2026-08-31]", "holdout: [2026-08-24]"),         # overlap
    PROTO.replace("holdout: [2026-08-31]", "holdout: [2026-08-20]"),         # holdout first
    PROTO.replace("holdout: [2026-08-31]", "holdout: [2026-08-26]"),         # excluded
])
def test_malformed_protocols_rejected(tmp_path, bad):
    p = tmp_path / "p.yaml"
    p.write_text(bad)
    with pytest.raises(ProtocolError):
        load_protocol(p)


def test_edit_changes_the_hash(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(PROTO)
    a = load_protocol(p).sha256
    p.write_text(PROTO + "\n# edited\n")
    assert load_protocol(p).sha256 != a


def test_config_count_ignores_repeat_runs_of_one_config(proto, tmp_path):
    log = tmp_path / "c.jsonl"
    assert count_configs(log) == 0
    log_config(proto, {"config": {"hs": 6}, "days": ["2026-08-24"]}, log_path=log)
    log_config(proto, {"config": {"hs": 6}, "days": ["2026-08-25"]}, log_path=log)
    assert log_config(proto, {"config": {"hs": 4}, "days": ["2026-08-24"]}, log_path=log) == 2


def test_the_real_frozen_protocol():
    p = load_protocol(PROTOCOL_PATH)
    assert date(2026, 8, 26) in p.excluded
    assert set(p.develop).isdisjoint(p.holdout)
    assert max(p.develop) < min(p.holdout)
    assert p.baseline["anchor"] == "parity"
