"""Safety invariants exercised against real independent SQLite connections."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4

import pytest

from gateway.ledger import Ledger, LedgerError, Limits, MAX_AMOUNT_NS


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc).timestamp()
RELAXED = Limits(max_active=200, max_session_active=200,
                 max_requests_per_window=200, max_session_requests_per_window=200,
                 max_ip_requests_per_window=200)


def hashed(value):
    return sha256(str(value).encode()).hexdigest()


def admit(ledger, *, session="session", ip="ip", reserve=30, cap=100,
          now=NOW, mode="hosted", request=None, project="demo"):
    return ledger.admit(project, "image", mode, hashed(session), hashed(ip),
                        request or str(uuid4()), reserve, cap, now)


def expect(code, function, *args, **kwargs):
    with pytest.raises(LedgerError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "private-ledger" / "usage.sqlite3"


def test_reserves_full_cost_and_only_verified_settlement_releases_difference(db_path):
    ledger = Ledger(db_path, RELAXED)
    ticket = admit(ledger, reserve=80)
    assert ledger.status(100, NOW)["remaining_ns"] == 20
    expect("budget_exceeded", admit, ledger, reserve=21)
    expect("invalid_transition", ledger.settle, ticket, 10, NOW)
    assert ledger.status(100, NOW)["held_ns"] == 80
    ledger.submitted(ticket, NOW)
    ledger.settle(ticket, 17, NOW + 1)
    assert ledger.status(100, NOW + 1) == {
        "utc_day": "2026-10-28", "daily_limit_ns": 100, "held_ns": 0,
        "settled_today_ns": 17, "committed_ns": 17, "remaining_ns": 83,
        "active_requests": 0,
    }
    admit(ledger, reserve=83, now=NOW + 1)
    expect("budget_exceeded", admit, ledger, reserve=1, now=NOW + 1)


@pytest.mark.parametrize("receipt", [-1, True, 2.5, "3", MAX_AMOUNT_NS + 1, 81])
def test_invalid_receipt_preserves_entire_hold(db_path, receipt):
    ledger = Ledger(db_path, RELAXED)
    ticket = admit(ledger, reserve=80)
    ledger.submitted(ticket, NOW)
    with pytest.raises(LedgerError):
        ledger.settle(ticket, receipt, NOW + 1)
    assert ledger.status(100, NOW + 1)["held_ns"] == 80
    assert ledger.status(100, NOW + 1)["settled_today_ns"] == 0


def test_uncertainty_restart_and_elapsed_lease_never_release_money(db_path):
    ledger = Ledger(db_path)
    ticket = admit(ledger, reserve=100)
    ledger.submitted(ticket, NOW)
    ledger.uncertain(ticket, NOW + 1)
    later = NOW + 365 * 86400
    restarted = Ledger(db_path)
    assert restarted.status(100, later)["held_ns"] == 100
    assert restarted.status(100, later)["active_requests"] == 0
    expect("budget_exceeded", admit, restarted, reserve=1, now=later)
    expect("invalid_transition", restarted.release_unsent, ticket, later)
    restarted.settle(ticket, 25, later)
    assert restarted.status(100, later)["settled_today_ns"] == 25
    assert restarted.status(100, later)["held_ns"] == 0


def test_crash_before_and_after_submission_keeps_both_reservations(db_path):
    ledger = Ledger(db_path, RELAXED)
    admit(ledger, reserve=40)
    ticket = admit(ledger, reserve=60)
    ledger.submitted(ticket, NOW)
    restarted = Ledger(db_path, RELAXED)
    assert restarted.status(100, NOW + 86400)["held_ns"] == 100
    expect("budget_exceeded", admit, restarted, reserve=1, now=NOW + 86400)


def test_utc_rollover_carries_pending_and_charges_receipt_day(db_path):
    ledger = Ledger(db_path, RELAXED)
    before = datetime(2026, 10, 28, 23, 59, 50, tzinfo=timezone.utc).timestamp()
    next_day = before + 20
    old = admit(ledger, reserve=20, now=before)
    ledger.submitted(old, before)
    ledger.settle(old, 20, before)
    pending = admit(ledger, reserve=70, now=before)
    ledger.submitted(pending, before)
    ledger.uncertain(pending, before)
    status = ledger.status(100, next_day)
    assert status["held_ns"] == 70
    assert status["settled_today_ns"] == 0
    assert status["remaining_ns"] == 30
    ledger.settle(pending, 60, next_day)
    assert ledger.status(100, next_day)["remaining_ns"] == 40
    # An identical receipt seen again next day keeps its original receipt date.
    ledger.settle(pending, 60, next_day + 86400)
    assert ledger.status(100, next_day + 86400)["settled_today_ns"] == 0


def test_atomic_dedupe_survives_settlement_release_and_restart(db_path):
    ledger = Ledger(db_path, RELAXED)
    request = str(uuid4())
    ticket = admit(ledger, request=request)
    expect("duplicate_request", admit, Ledger(db_path, RELAXED), request=request)
    ledger.release_unsent(ticket, NOW)
    expect("duplicate_request", admit, Ledger(db_path, RELAXED), request=request, now=NOW + 86400)
    # Dedupe covers the session, independent of project, and UUID formatting.
    expect("duplicate_request", admit, ledger, request=request.upper(), project="other")
    admit(ledger, request=request, session="other-session")


def _attempt_thread(path, number, *, reserve, cap, same_request=None):
    ledger = Ledger(path, RELAXED)
    try:
        return admit(ledger, session=number, ip=number, reserve=reserve, cap=cap,
                     request=same_request)
    except LedgerError as error:
        return error.code


def test_parallel_independent_instances_share_one_cross_project_budget(db_path):
    Ledger(db_path, RELAXED)

    def attempt(number):
        ledger = Ledger(db_path, RELAXED)
        try:
            return admit(ledger, session=number, ip=number, project=f"project-{number}",
                         reserve=30, cap=100)
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(24)))
    assert results.count("budget_exceeded") == 21
    assert Ledger(db_path, RELAXED).status(100, NOW)["held_ns"] == 90


def _process_admit(arguments):
    path, number = arguments
    return _attempt_thread(path, number, reserve=30, cap=100)


def test_multiple_processes_cannot_overspend(db_path):
    Ledger(db_path, RELAXED)
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_process_admit, [(str(db_path), n) for n in range(12)]))
    assert results.count("budget_exceeded") == 9
    assert Ledger(db_path, RELAXED).status(100, NOW)["held_ns"] == 90


def test_parallel_duplicate_is_admitted_once(db_path):
    Ledger(db_path, RELAXED)
    request = str(uuid4())

    def attempt(_):
        try:
            return admit(Ledger(db_path, RELAXED), request=request)
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(20)))
    assert results.count("duplicate_request") == 19
    assert Ledger(db_path, RELAXED).status(100, NOW)["held_ns"] == 30


def test_parallel_global_active_cap_is_atomic(db_path):
    Ledger(db_path)

    def attempt(n):
        try:
            return admit(Ledger(db_path), session=n, ip=n, reserve=0, mode="own")
        except LedgerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(12)))
    assert results.count("global_concurrency_limit") == 8
    assert Ledger(db_path).status(100, NOW)["active_requests"] == 4


def test_per_session_active_cap_and_unsent_release(db_path):
    ledger = Ledger(db_path)
    ticket = admit(ledger)
    expect("session_concurrency_limit", admit, ledger)
    ledger.release_unsent(ticket, NOW)
    assert ledger.status(100, NOW)["held_ns"] == 0
    admit(ledger)


def test_active_lease_persists_restart_and_cannot_start_late(db_path):
    ledger = Ledger(db_path)
    old = admit(ledger)
    expect("session_concurrency_limit", admit, Ledger(db_path), now=NOW + 119)
    expect("lease_expired", ledger.submitted, old, NOW + 120)
    assert ledger.status(100, NOW + 120)["held_ns"] == 30
    new = admit(Ledger(db_path), now=NOW + 120)
    ledger.submitted(new, NOW + 120)


@pytest.mark.parametrize("kind,expected", [
    ("global", "global_rate_limit"),
    ("session", "session_rate_limit"),
    ("ip", "ip_rate_limit"),
])
def test_rates_count_all_admitted_calls_and_persist_restart(db_path, kind, expected):
    settings = {
        "global": {"max_requests_per_window": 2},
        "session": {"max_session_requests_per_window": 2},
        "ip": {"max_ip_requests_per_window": 2},
    }
    limits = replace(RELAXED, **settings[kind])
    ledger = Ledger(db_path, limits)
    for n in range(2):
        ticket = admit(ledger, session="session" if kind == "session" else n,
                       ip="ip" if kind == "ip" else n)
        ledger.release_unsent(ticket, NOW)
    restarted = Ledger(db_path, limits)
    expect(expected, admit, restarted)
    admit(restarted, now=NOW + 60)


def test_own_key_has_zero_host_reservation_and_same_resource_and_rate_caps(db_path):
    ledger = Ledger(db_path, replace(RELAXED, max_session_requests_per_window=2, max_session_active=1))
    hosted = admit(ledger, session="hosted", reserve=100)
    own = admit(ledger, reserve=0, mode="own", cap=0)
    assert ledger.status(100, NOW)["held_ns"] == 100
    expect("session_concurrency_limit", admit, ledger, reserve=0, mode="own")
    ledger.submitted(own, NOW)
    expect("invalid_settlement", ledger.settle, own, 1, NOW)
    ledger.settle(own, 0, NOW)
    second = admit(ledger, reserve=0, mode="own", cap=0)
    ledger.release_unsent(second, NOW)
    expect("session_rate_limit", admit, ledger, reserve=0, mode="own", cap=0)
    expect("invalid_mode", admit, ledger, reserve=1, mode="own")
    assert ledger.status(100, NOW)["held_ns"] == 100
    ledger.release_unsent(hosted, NOW)


def test_settlement_and_unsent_release_state_machine_fail_closed(db_path):
    ledger = Ledger(db_path, RELAXED)
    ticket = admit(ledger, reserve=80)
    ledger.submitted(ticket, NOW)
    expect("invalid_transition", ledger.release_unsent, ticket, NOW)
    ledger.uncertain(ticket, NOW)
    ledger.uncertain(ticket, NOW)
    expect("invalid_transition", ledger.submitted, ticket, NOW)
    assert ledger.status(100, NOW)["held_ns"] == 80
    ledger.settle(ticket, 30, NOW)
    ledger.settle(ticket, 30, NOW)
    expect("settlement_mismatch", ledger.settle, ticket, 20, NOW)
    expect("invalid_transition", ledger.uncertain, ticket, NOW)
    expect("invalid_transition", ledger.release_unsent, ticket, NOW)
    assert ledger.status(100, NOW)["settled_today_ns"] == 30


@pytest.mark.parametrize("amount", [-1, True, 0.1, "20", MAX_AMOUNT_NS + 1])
def test_invalid_reservation_and_cap_are_rejected_without_records(db_path, amount):
    ledger = Ledger(db_path)
    expect("invalid_amount", admit, ledger, reserve=amount)
    expect("invalid_amount", admit, ledger, cap=amount)
    assert ledger.status(100, NOW)["active_requests"] == 0


@pytest.mark.parametrize("bad_time", [-1, True, float("nan"), float("inf"), 10**1000])
def test_invalid_time_is_rejected(db_path, bad_time):
    ledger = Ledger(db_path)
    expect("invalid_time", admit, ledger, now=bad_time)


def test_raw_ip_prompt_like_metadata_and_non_uuid_request_are_rejected(db_path):
    ledger = Ledger(db_path)
    expect("invalid_hash", ledger.admit, "demo", "image", "hosted", hashed("session"),
           "192.168.1.1", str(uuid4()), 10, 100, NOW)
    expect("invalid_metadata", admit, ledger, project="a prompt with spaces")
    expect("invalid_request_id", admit, ledger, request="request-one")


def test_status_and_database_store_only_metadata(db_path):
    ledger = Ledger(db_path)
    admit(ledger)
    assert set(ledger.status(100, NOW)) == {
        "utc_day", "daily_limit_ns", "held_ns", "settled_today_ns",
        "committed_ns", "remaining_ns", "active_requests",
    }
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(tickets)")}
        assert columns == {"ticket_id", "project", "task", "mode", "session_hash", "ip_hash",
                           "request_id", "reserved_ns", "state", "admitted_at", "active_until",
                           "actual_ns", "receipt_day", "updated_at"}
        assert "192.168" not in json.dumps(db.execute("SELECT * FROM tickets").fetchall())


def test_private_directory_and_file_modes_are_created_and_rechecked(db_path):
    ledger = Ledger(db_path)
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    db_path.chmod(0o644)
    expect("unsafe_path", ledger.status, 100, NOW)
    expect("unsafe_path", Ledger, db_path)


def test_existing_public_parent_is_rejected(tmp_path):
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    expect("unsafe_path", Ledger, parent / "ledger.sqlite3")


def test_symlink_file_and_ancestor_and_hardlink_are_rejected(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    actual = private / "actual.sqlite3"
    Ledger(actual)
    linked = private / "linked.sqlite3"
    linked.symlink_to(actual)
    expect("unsafe_path", Ledger, linked)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    expect("unsafe_path", Ledger, alias / "another.sqlite3")
    hardlink = private / "hard.sqlite3"
    os.link(actual, hardlink)
    expect("unsafe_path", Ledger, hardlink)
    expect("unsafe_path", Ledger, actual)


def test_source_tree_and_relative_paths_are_rejected():
    source_path = Path(__file__).resolve().parent.parent / "forbidden.sqlite3"
    expect("unsafe_path", Ledger, source_path)
    expect("unsafe_path", Ledger, Path("relative.sqlite3"))
    assert not source_path.exists()


def test_lowered_cap_blocks_new_host_spend_and_masks_available_as_zero(db_path):
    ledger = Ledger(db_path, RELAXED)
    admit(ledger, reserve=100, cap=100)
    assert ledger.status(5, NOW)["remaining_ns"] == 0
    expect("budget_exceeded", admit, ledger, reserve=0, cap=5)
    admit(ledger, session="own", reserve=0, mode="own", cap=5)


def test_sqlite_max_integer_boundary_uses_exact_integer_arithmetic(db_path):
    ledger = Ledger(db_path, RELAXED)
    ticket = admit(ledger, reserve=MAX_AMOUNT_NS, cap=MAX_AMOUNT_NS)
    expect("budget_exceeded", admit, ledger, reserve=1, cap=MAX_AMOUNT_NS)
    ledger.submitted(ticket, NOW)
    ledger.settle(ticket, MAX_AMOUNT_NS - 1, NOW)
    admit(ledger, reserve=1, cap=MAX_AMOUNT_NS)
    assert ledger.status(MAX_AMOUNT_NS, NOW)["remaining_ns"] == 0
