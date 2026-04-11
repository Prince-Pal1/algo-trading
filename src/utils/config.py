"""Config loader — reads TOML files from config/ directory.

Usage:
    from src.utils.config import get_config
    cfg = get_config()
    print(cfg.settings["general"]["mode"])  # "paper"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _load_toml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class Config:
    settings: dict = field(default_factory=dict)
    exchanges: dict = field(default_factory=dict)
    strategies: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    logging: dict = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return self.settings.get("general", {}).get("mode", "paper")

    @property
    def log_level(self) -> str:
        return self.settings.get("general", {}).get("log_level", "INFO")

    @property
    def redis_enabled(self) -> bool:
        return self.settings.get("redis", {}).get("enabled", False)

    @property
    def redis_host(self) -> str:
        return self.settings.get("redis", {}).get("host", "localhost")

    @property
    def redis_port(self) -> int:
        return self.settings.get("redis", {}).get("port", 6379)

    @property
    def db_path(self) -> str:
        return self.settings.get("database", {}).get("path", "data/trades.db")

    def get_exchange(self, name: str) -> dict:
        """Get exchange config, with env var overrides."""
        base = self.exchanges.get(name, {})
        # Allow env var overrides: BINANCE_API_KEY, ALPACA_API_KEY, etc.
        prefix = name.upper()
        for key in list(base.keys()):
            env_key = f"{prefix}_{key.upper()}"
            env_val = os.environ.get(env_key)
            if env_val is not None:
                base[key] = env_val
        return base

    def get_strategy(self, name: str) -> dict:
        return self.strategies.get(name, {})


_config: Config | None = None


def get_config(reload: bool = False) -> Config:
    """Singleton config loader. Call with reload=True to re-read files."""
    global _config
    if _config is None or reload:
        _config = Config(
            settings=_load_toml("settings.toml"),
            exchanges=_load_toml("exchanges.toml"),
            strategies=_load_toml("strategies.toml"),
            risk=_load_toml("risk.toml"),
            logging=_load_toml("logging.toml"),
        )
    return _config
