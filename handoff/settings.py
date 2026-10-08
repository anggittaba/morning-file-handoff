"""Settings from environment variables (or a local .env that git ignores).

Every value has a default that works with the bundled simulator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader: KEY=VALUE lines, existing environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


@dataclass
class Settings:
    var: Path = field(default_factory=lambda: Path(os.getenv("HANDOFF_VAR", ROOT / "var")))
    db: Path | None = None
    topology: Path = field(default_factory=lambda: Path(os.getenv("HANDOFF_TOPOLOGY", ROOT / "config" / "topology.json")))
    branches: Path = field(default_factory=lambda: Path(os.getenv("HANDOFF_BRANCHES", ROOT / "config" / "branches.csv")))
    credentials: Path | None = None
    known_hosts: Path | None = None
    window_start: tuple[int, int] = field(default_factory=lambda: _hm(os.getenv("HANDOFF_START", "06:00")))
    window_end: tuple[int, int] = field(default_factory=lambda: _hm(os.getenv("HANDOFF_END", "07:45")))
    deadline: tuple[int, int] = field(default_factory=lambda: _hm(os.getenv("HANDOFF_DEADLINE", "07:30")))
    interval_min: float = field(default_factory=lambda: float(os.getenv("HANDOFF_INTERVAL_MIN", "5")))
    at_risk_min: int = field(default_factory=lambda: int(os.getenv("HANDOFF_AT_RISK_MIN", "45")))
    upload_attempts: int = field(default_factory=lambda: int(os.getenv("HANDOFF_UPLOAD_ATTEMPTS", "3")))
    retry_backoff_s: float = field(default_factory=lambda: float(os.getenv("HANDOFF_RETRY_BACKOFF_S", "0.5")))
    connect_timeout_s: float = field(default_factory=lambda: float(os.getenv("HANDOFF_CONNECT_TIMEOUT_S", "8")))
    concurrency: int = field(default_factory=lambda: int(os.getenv("HANDOFF_CONCURRENCY", "6")))
    operator: str = field(default_factory=lambda: os.getenv("HANDOFF_OPERATOR", "operator"))

    def __post_init__(self) -> None:
        self.var = Path(self.var)
        if self.db is None:
            self.db = Path(os.getenv("HANDOFF_DB", self.var / "handoff.db"))
        if self.credentials is None:
            self.credentials = Path(os.getenv("HANDOFF_CREDENTIALS", self.var / "sim" / "credentials.json"))
        if self.known_hosts is None:
            self.known_hosts = Path(os.getenv("HANDOFF_KNOWN_HOSTS", self.var / "sim" / "known_hosts"))

    @property
    def work(self) -> Path:
        return self.var / "work"

    @property
    def sim(self) -> Path:
        return self.var / "sim"
