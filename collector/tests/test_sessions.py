from datetime import datetime, timedelta, timezone

from piquiti.sessions import Session, sessionize

T0 = datetime(2026, 9, 23, 2, 0, tzinfo=timezone.utc)
GAP = timedelta(minutes=10)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_gap_boundary_splits_sessions():
    rows = [(at(0), "10.0.0.5", "Netflix"), (at(10), "10.0.0.5", "Netflix"), (at(20.01), "10.0.0.5", "Netflix")]
    out = sorted(sessionize(rows, {}, GAP), key=lambda s: s.start_ts)
    assert [(s.start_ts, s.last_query, s.query_count) for s in out] == [(at(0), at(10), 2), (at(20.01), at(20.01), 1)]


def test_devices_and_services_are_separate():
    rows = [(at(0), "a", "Netflix"), (at(1), "b", "Netflix"), (at(2), "a", "YouTube")]
    assert len(sessionize(rows, {}, GAP)) == 3


def test_extends_stored_session_across_runs():
    stored = Session("a", "Netflix", at(0), at(5), 3, original_start=at(0))
    latest = {("a", "Netflix"): stored}
    out = sessionize([(at(12), "a", "Netflix")], latest, GAP)
    assert out == [stored]
    assert (stored.last_query, stored.query_count) == (at(12), 4)


def test_late_row_does_not_replace_newer_session():
    stored = Session("a", "Netflix", at(60), at(70), 2, original_start=at(60))
    latest = {("a", "Netflix"): stored}
    out = sessionize([(at(0), "a", "Netflix")], latest, GAP)
    assert len(out) == 1 and out[0] is not stored
    assert latest[("a", "Netflix")] is stored


def test_end_includes_tail():
    s = Session("a", "x", at(0), at(5), 2)
    assert s.end_ts(timedelta(minutes=2)) == at(7)
