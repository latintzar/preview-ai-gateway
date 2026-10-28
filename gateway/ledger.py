"""Private, shared SQLite admission ledger. Amounts are integer nanodollars.

The caller owns provider-receipt verification. ``submitted`` MUST commit before
any outbound provider call; ``settle`` is called only with verified actual usage
for that ticket. An ambiguous outcome retains its entire reservation indefinitely.
Resource leases expire independently of monetary reservations.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator
from uuid import UUID, uuid4


MAX_AMOUNT_NS = (1 << 63) - 1
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z")
_HELD_STATES = ("admitted", "submitted", "uncertain")


class LedgerError(Exception):
    """Safe, payload-free error suitable for mapping to an API response."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Limits:
    max_active: int = 4
    max_session_active: int = 1
    max_requests_per_window: int = 20
    max_session_requests_per_window: int = 4
    max_ip_requests_per_window: int = 8
    window_seconds: float = 60
    active_lease_seconds: float = 120
    sqlite_timeout_seconds: float = 10

    def __post_init__(self) -> None:
        for name in (
            "max_active", "max_session_active", "max_requests_per_window",
            "max_session_requests_per_window", "max_ip_requests_per_window",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("window_seconds", "active_lease_seconds", "sqlite_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


def _amount(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_AMOUNT_NS:
        raise LedgerError("invalid_amount")
    return value


def _clock(value: float) -> float:
    if type(value) not in (int, float) or value < 0:
        raise LedgerError("invalid_time")
    try:
        if not math.isfinite(value):
            raise ValueError
        datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise LedgerError("invalid_time") from None
    return float(value)


def _day(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).date().isoformat()


class Ledger:
    """All projects and processes MUST share this one file and daily-limit config.

    The database belongs in a dedicated private directory outside the source
    tree. Existing permissions are checked rather than silently weakened or
    repaired. Each operation uses a separate connection and an IMMEDIATE write
    transaction, making admission safe across independent instances/processes.
    """

    def __init__(self, path: str | Path, limits: Limits | None = None):
        self.limits = limits or Limits()
        self.path = Path(path)
        self._prepare_path()
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise LedgerError("unsupported_schema")
            db.execute(
                """CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    task TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('hosted', 'own')),
                    session_hash TEXT NOT NULL,
                    ip_hash TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    reserved_ns INTEGER NOT NULL CHECK(reserved_ns >= 0),
                    state TEXT NOT NULL CHECK(state IN
                        ('admitted','submitted','uncertain','settled','released')),
                    admitted_at REAL NOT NULL,
                    active_until REAL NOT NULL,
                    actual_ns INTEGER CHECK(actual_ns >= 0 AND actual_ns <= reserved_ns),
                    receipt_day TEXT,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_hash, request_id),
                    CHECK(mode != 'own' OR reserved_ns = 0),
                    CHECK((state = 'settled' AND actual_ns IS NOT NULL
                           AND receipt_day IS NOT NULL)
                          OR (state != 'settled' AND actual_ns IS NULL
                              AND receipt_day IS NULL))
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS tickets_admitted ON tickets(admitted_at)")
            db.execute("CREATE INDEX IF NOT EXISTS tickets_session ON tickets(session_hash, admitted_at)")
            db.execute("CREATE INDEX IF NOT EXISTS tickets_ip ON tickets(ip_hash, admitted_at)")
            db.execute("CREATE INDEX IF NOT EXISTS tickets_receipt ON tickets(receipt_day)")
            db.execute("PRAGMA user_version = 1")

    def _check_ancestors(self) -> None:
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise LedgerError("unsafe_path")
        # Reject links anywhere in the path, including ancestors and the DB.
        current = Path(self.path.anchor)
        for component in self.path.parts[1:]:
            current /= component
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise LedgerError("unsafe_path")
        source_root = Path(__file__).resolve().parent.parent
        if self.path == source_root or source_root in self.path.parents:
            raise LedgerError("unsafe_path")

    def _prepare_path(self) -> None:
        try:
            self._check_ancestors()
            try:
                self.path.parent.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self._check_ancestors()
            self._check_directory()
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            self._check_private_path()
        except LedgerError:
            raise
        except OSError:
            raise LedgerError("unsafe_path") from None

    def _check_directory(self) -> None:
        info = self.path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise LedgerError("unsafe_path")

    def _check_private_path(self) -> None:
        self._check_ancestors()
        self._check_directory()
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise LedgerError("unsafe_path")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            self._check_private_path()
            db = sqlite3.connect(self.path, timeout=self.limits.sqlite_timeout_seconds,
                                 isolation_level=None)
            db.row_factory = sqlite3.Row
            # DELETE journal avoids a persistent extra database/WAL file. FULL
            # keeps committed reservations durable before the outbound send.
            db.execute("PRAGMA synchronous = FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except LedgerError:
            if db is not None:
                db.rollback()
            raise
        except (sqlite3.Error, OSError):
            if db is not None:
                db.rollback()
            raise LedgerError("ledger_unavailable") from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _totals(db: sqlite3.Connection, now: float) -> tuple[int, int, int]:
        held = sum(row[0] for row in db.execute(
            "SELECT reserved_ns FROM tickets WHERE state IN ('admitted','submitted','uncertain')"
        ))
        settled = sum(row[0] for row in db.execute(
            "SELECT actual_ns FROM tickets WHERE state = 'settled' AND receipt_day = ?", (_day(now),)
        ))
        active = db.execute(
            "SELECT count(*) FROM tickets WHERE state IN ('admitted','submitted','uncertain') AND active_until > ?",
            (now,),
        ).fetchone()[0]
        return held, settled, active

    def admit(self, project: str, task: str, mode: str, session_hash: str,
              ip_hash: str, request_id: str, reserved_ns: int,
              daily_limit_ns: int, now: float) -> str:
        reserved_ns, daily_limit_ns, now = _amount(reserved_ns), _amount(daily_limit_ns), _clock(now)
        if not all(type(value) is str and _LABEL.fullmatch(value) for value in (project, task)):
            raise LedgerError("invalid_metadata")
        if not all(type(value) is str and _HASH.fullmatch(value) for value in (session_hash, ip_hash)):
            raise LedgerError("invalid_hash")
        if mode not in ("hosted", "own") or (mode == "own" and reserved_ns != 0):
            raise LedgerError("invalid_mode")
        try:
            if type(request_id) is not str:
                raise ValueError
            request_id = str(UUID(request_id))
        except (ValueError, AttributeError):
            raise LedgerError("invalid_request_id") from None
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM tickets WHERE session_hash = ? AND request_id = ?",
                          (session_hash, request_id)).fetchone():
                raise LedgerError("duplicate_request")
            # Future timestamps remain counted if the wall clock moves back.
            since = now - self.limits.window_seconds
            for condition, parameters, maximum, error in (
                ("", (), self.limits.max_requests_per_window, "global_rate_limit"),
                (" AND session_hash = ?", (session_hash,), self.limits.max_session_requests_per_window,
                 "session_rate_limit"),
                (" AND ip_hash = ?", (ip_hash,), self.limits.max_ip_requests_per_window, "ip_rate_limit"),
            ):
                count = db.execute("SELECT count(*) FROM tickets WHERE admitted_at > ?" + condition,
                                   (since, *parameters)).fetchone()[0]
                if count >= maximum:
                    raise LedgerError(error)
            held, settled, active = self._totals(db, now)
            if active >= self.limits.max_active:
                raise LedgerError("global_concurrency_limit")
            session_active = db.execute(
                "SELECT count(*) FROM tickets WHERE session_hash = ? AND state IN "
                "('admitted','submitted','uncertain') AND active_until > ?", (session_hash, now)
            ).fetchone()[0]
            if session_active >= self.limits.max_session_active:
                raise LedgerError("session_concurrency_limit")
            # Own-key calls carry no host spend and still obey all resource caps.
            if mode == "hosted" and held + settled + reserved_ns > daily_limit_ns:
                raise LedgerError("budget_exceeded")
            ticket_id = str(uuid4())
            db.execute(
                "INSERT INTO tickets (ticket_id, project, task, mode, session_hash, ip_hash, request_id, "
                "reserved_ns, state, admitted_at, active_until, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?, ?, ?)",
                (ticket_id, project, task, mode, session_hash, ip_hash, request_id, reserved_ns,
                 now, now + self.limits.active_lease_seconds, now),
            )
        return ticket_id

    @staticmethod
    def _ticket(db: sqlite3.Connection, ticket_id: str) -> sqlite3.Row:
        if type(ticket_id) is not str:
            raise LedgerError("invalid_ticket")
        ticket = db.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if ticket is None:
            raise LedgerError("invalid_ticket")
        return ticket

    def submitted(self, ticket_id: str, now: float | None = None) -> None:
        now = _clock(time.time() if now is None else now)
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id)
            if ticket["state"] != "admitted":
                raise LedgerError("invalid_transition")
            if now < ticket["admitted_at"]:
                raise LedgerError("invalid_time")
            if now >= ticket["active_until"]:
                raise LedgerError("lease_expired")
            db.execute("UPDATE tickets SET state = 'submitted', updated_at = ? WHERE ticket_id = ?",
                       (now, ticket_id))

    def settle(self, ticket_id: str, actual_ns: int, now: float) -> None:
        """Replace a hold with verified receipt usage, charged on its UTC day.

        A repeated identical receipt is idempotent. An invalid or conflicting
        receipt never releases a pending hold. The provider adapter must bind the
        verified receipt to the admitted ticket before invoking this method.
        """
        actual_ns, now = _amount(actual_ns), _clock(now)
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id)
            if actual_ns > ticket["reserved_ns"]:
                raise LedgerError("invalid_settlement")
            if ticket["state"] == "settled":
                if ticket["actual_ns"] != actual_ns:
                    raise LedgerError("settlement_mismatch")
                return
            if ticket["state"] not in ("submitted", "uncertain"):
                raise LedgerError("invalid_transition")
            if now < ticket["admitted_at"]:
                raise LedgerError("invalid_time")
            db.execute(
                "UPDATE tickets SET state = 'settled', actual_ns = ?, receipt_day = ?, updated_at = ? "
                "WHERE ticket_id = ?", (actual_ns, _day(now), now, ticket_id)
            )

    def uncertain(self, ticket_id: str, now: float) -> None:
        now = _clock(now)
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id)
            if ticket["state"] not in ("submitted", "uncertain"):
                raise LedgerError("invalid_transition")
            if now < ticket["admitted_at"]:
                raise LedgerError("invalid_time")
            db.execute("UPDATE tickets SET state = 'uncertain', updated_at = ? WHERE ticket_id = ?",
                       (now, ticket_id))

    def release_unsent(self, ticket_id: str, now: float) -> None:
        now = _clock(now)
        with self._transaction() as db:
            ticket = self._ticket(db, ticket_id)
            if ticket["state"] != "admitted":
                raise LedgerError("invalid_transition")
            if now < ticket["admitted_at"]:
                raise LedgerError("invalid_time")
            db.execute("UPDATE tickets SET state = 'released', updated_at = ? WHERE ticket_id = ?",
                       (now, ticket_id))

    def status(self, daily_limit_ns: int, now: float) -> dict[str, int | str]:
        daily_limit_ns, now = _amount(daily_limit_ns), _clock(now)
        with self._transaction() as db:
            held, settled, active = self._totals(db, now)
        return {
            "utc_day": _day(now),
            "daily_limit_ns": daily_limit_ns,
            "held_ns": held,
            "settled_today_ns": settled,
            "committed_ns": held + settled,
            "remaining_ns": max(0, daily_limit_ns - held - settled),
            "active_requests": active,
        }
