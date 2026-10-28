from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat
import threading


class SessionError(Exception):
    pass


@dataclass
class Session:
    id: str = field(repr=False)
    csrf: str = field(repr=False)
    expires: float
    key: bytearray = field(default_factory=bytearray, repr=False)
    key_expires: float = 0

    def erase_key(self):
        self.key[:] = b"\0" * len(self.key)
        self.key.clear()
        self.key_expires = 0


def _load_ip_salt(ledger_path: Path) -> bytes:
    """Read one durable IP-only salt from the ledger's private directory.

    Session identifiers/keys remain disposable. A file lock serializes first
    creation across processes; partial or unsafe existing files fail closed.
    """
    path = ledger_path.with_name(ledger_path.name + ".ip-salt")
    fd = None
    try:
        current = Path(path.anchor)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError()
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ValueError()
        directory = path.parent.stat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid()
                or stat.S_IMODE(directory.st_mode) != 0o700):
            raise ValueError()
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError()
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_size == 0:
            value = secrets.token_bytes(32)
            remaining = memoryview(value)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError()
                remaining = remaining[written:]
            os.fsync(fd)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.lseek(fd, 0, os.SEEK_SET)
        value = os.read(fd, 33)
        if len(value) != 32:
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise ValueError("Use a private owned mode-0600 IP salt beside the ledger.") from None
    finally:
        if fd is not None:
            os.close(fd)


class Sessions:
    def __init__(self, config, clock):
        self.config, self.clock = config, clock
        self._items = {}
        self._buckets = {}
        self._salt = secrets.token_bytes(32)
        self._ip_salt = _load_ip_salt(config.ledger_path)
        self._lock = threading.RLock()

    def digest(self, value):
        return hmac.new(self._salt, value.encode(), hashlib.sha256).hexdigest()

    def ip_digest(self, value):
        return hmac.new(self._ip_salt, value.encode(), hashlib.sha256).hexdigest()

    def _prune(self, now):
        for token, item in list(self._items.items()):
            if item.key and now >= item.key_expires:
                item.erase_key()
            if now >= item.expires:
                item.erase_key(); del self._items[token]
        self._buckets = {key: value for key, value in self._buckets.items() if now - value[0] < 60}

    def control_rate(self, scope, limit):
        with self._lock:
            now = self.clock(); self._prune(now)
            started, count = self._buckets.get(scope, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            if count >= limit:
                raise SessionError("control-rate-limit")
            self._buckets[scope] = (started, count + 1)

    def get(self, token):
        with self._lock:
            self._prune(self.clock())
            item = self._items.get(token or "")
            if not item:
                raise SessionError("session-required")
            return item

    def prune(self):
        with self._lock:
            self._prune(self.clock())

    def bootstrap(self, token, ip):
        with self._lock:
            try:
                item = self.get(token)
            except SessionError:
                item = None
            if item:
                self.control_rate("status-global", 120)
                self.control_rate("status-session:" + self.digest(token), 30)
                return item, False
            self.control_rate("bootstrap-global", 60)
            self.control_rate("bootstrap-ip:" + self.ip_digest(ip), 10)
            if len(self._items) >= self.config.max_sessions:
                raise SessionError("session-capacity")
            item = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock() + self.config.session_seconds)
            self._items[item.id] = item
            return item, True

    def put_key(self, token, value):
        with self._lock:
            item = self.get(token); self.control_rate("key:" + self.digest(token), 3)
            item.erase_key(); item.key.extend(value.encode()); item.key_expires = min(item.expires, self.clock() + self.config.visitor_key_seconds)

    def key_for(self, token):
        with self._lock:
            item = self.get(token)
            if not item.key:
                raise SessionError("key-required")
            return item.key.decode()

    def forget_key(self, token):
        with self._lock:
            self.get(token).erase_key()

    def close(self):
        with self._lock:
            for item in self._items.values():
                item.erase_key()
            self._items.clear(); self._buckets.clear()
