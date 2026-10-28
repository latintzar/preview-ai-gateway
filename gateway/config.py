from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
import os

NANODOLLARS = 1_000_000_000
APPROVED_DAILY_LIMIT_NS = 2 * NANODOLLARS


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or "*" in value):
        raise ValueError("Configure an exact HTTPS origin.")
    port = parsed.port
    return "https://" + parsed.hostname.lower() + (f":{port}" if port and port != 443 else "")


@dataclass(frozen=True)
class Config:
    public_origin: str = "https://ai.example.test"
    site_domain: str = "example.test"
    ledger_path: Path = field(default_factory=lambda: Path.home() / ".local/state/atelier-preview/budget.sqlite3")
    shared_enabled: bool = False
    daily_limit_ns: int = APPROVED_DAILY_LIMIT_NS
    shared_key: str = field(default="", repr=False)
    session_seconds: int = 1800
    visitor_key_seconds: int = 600
    max_sessions: int = 1024

    def __post_init__(self):
        object.__setattr__(self, "public_origin", origin(self.public_origin))
        if not self.site_domain or self.site_domain.startswith(".") or "." not in self.site_domain:
            raise ValueError("Configure the site's registered domain.")
        self.check_site(self.public_origin)
        if type(self.daily_limit_ns) is not int or not 0 <= self.daily_limit_ns <= APPROVED_DAILY_LIMIT_NS:
            raise ValueError("Daily allowance must stay within the approved $2 limit.")
        if self.shared_enabled and (not self.shared_key or not self.daily_limit_ns):
            raise ValueError("Shared mode requires its Atelier key and explicit daily allowance.")
        if not 60 <= self.visitor_key_seconds <= 600 or not 60 <= self.session_seconds <= 1800:
            raise ValueError("Session lifetimes exceed the supported bounds.")
        if not 1 <= self.max_sessions <= 1024:
            raise ValueError("Session count exceeds the supported bound.")
        source = Path(__file__).resolve().parents[1]
        if self.ledger_path.absolute().resolve().is_relative_to(source):
            raise ValueError("Place the private ledger outside the source checkout.")

    def check_site(self, value: str):
        host = urlsplit(origin(value)).hostname
        if host != self.site_domain and not host.endswith("." + self.site_domain):
            raise ValueError("Preview and key gateway origins must use the configured site domain.")

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        try:
            amount = Decimal(env.get("ATELIER_DAILY_LIMIT_USD", "2"))
            if not amount.is_finite() or amount < 0 or amount * NANODOLLARS != (amount * NANODOLLARS).to_integral_value():
                raise ValueError()
            enabled = env.get("ATELIER_SHARED_AI_ENABLED", "false")
            if enabled not in {"true", "false"}:
                raise ValueError()
            return cls(public_origin=env.get("ATELIER_GATEWAY_ORIGIN", "https://ai.example.test"),
                       site_domain=env.get("ATELIER_SITE_DOMAIN", "example.test"),
                       ledger_path=Path(env.get("ATELIER_LEDGER_PATH", str(Path.home() / ".local/state/atelier-preview/budget.sqlite3"))),
                       shared_enabled=enabled == "true", daily_limit_ns=int(amount * NANODOLLARS),
                       shared_key=env.get("ATELIER_OPENAI_API_KEY", ""))
        except (InvalidOperation, ValueError):
            raise ValueError("Check Atelier gateway configuration names and bounds.") from None
