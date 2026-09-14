from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


@dataclass(slots=True)
class Settings:
    repo_root: Path = Path(__file__).resolve().parent.parent
    ingest_root: Path = Path("data/ingest")
    logs_root: Path = Path("logs")
    manual_mappings_path: Path = Path("config/manual_mappings.json")
    collect_exchanges: tuple[str, ...] = ("polymarket", "opn", "azuro", "sxbet", "myriad", "kalshi")
    max_markets_per_exchange: int = 50
    sports_only: bool = field(default_factory=lambda: _env_bool("SPORTS_ONLY", "false"))
    market_db_path: Path = field(default_factory=lambda: Path(os.getenv("MARKET_DB_PATH", "data/market_cache.sqlite3")))
    sports_db_path: Path = field(default_factory=lambda: Path(os.getenv("SPORTS_DB_PATH", "data/sports_cache.sqlite3")))
    discovery_interval_seconds: int = field(default_factory=lambda: int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "90")))
    quote_refresh_interval_seconds: int = field(default_factory=lambda: int(os.getenv("QUOTE_REFRESH_INTERVAL_SECONDS", "20")))
    cleanup_interval_seconds: int = field(default_factory=lambda: int(os.getenv("CLEANUP_INTERVAL_SECONDS", "600")))
    discovery_safety_cap_per_venue: int = field(
        default_factory=lambda: int(os.getenv("DISCOVERY_SAFETY_CAP_PER_VENUE", "10000"))
    )
    sports_auto_arb_families: tuple[str, ...] = field(
        default_factory=lambda: _env_list("SPORTS_AUTO_ARB_FAMILIES", "moneyline_2way,three_way,outright")
    )
    sports_linked_threshold: float = field(default_factory=lambda: float(os.getenv("SPORTS_LINKED_THRESHOLD", "0.80")))
    sports_review_threshold: float = field(default_factory=lambda: float(os.getenv("SPORTS_REVIEW_THRESHOLD", "0.60")))
    sports_match_window_hours: int = field(default_factory=lambda: int(os.getenv("SPORTS_MATCH_WINDOW_HOURS", "6")))
    sports_quote_priority_hours: int = field(default_factory=lambda: int(os.getenv("SPORTS_QUOTE_PRIORITY_HOURS", "72")))
    sports_digest_interval_seconds: int = field(default_factory=lambda: int(os.getenv("SPORTS_DIGEST_INTERVAL_SECONDS", "600")))
    sports_digest_market_limit: int = field(default_factory=lambda: int(os.getenv("SPORTS_DIGEST_MARKET_LIMIT", "5")))
    sports_digest_match_limit: int = field(default_factory=lambda: int(os.getenv("SPORTS_DIGEST_MATCH_LIMIT", "5")))
    quote_size_usd: float = 100.0
    min_match_confidence: float = 0.55
    min_live_match_confidence: float = 0.80
    min_profit_pct: float = 2.0
    quote_ttl_seconds: int = 300
    azuro_slippage_bps: int = 150
    opn_slippage_bps: int = field(default_factory=lambda: int(os.getenv("OPN_SLIPPAGE_BPS", "100")))
    kalshi_slippage_bps: int = field(default_factory=lambda: int(os.getenv("KALSHI_SLIPPAGE_BPS", "25")))
    sxbet_slippage_bps: int = field(default_factory=lambda: int(os.getenv("SXBET_SLIPPAGE_BPS", "50")))
    myriad_slippage_bps: int = 150
    myriad_default_fee_bps: int = 100
    polymarket_fee_bps: int = 0
    opn_fee_bps: int = field(default_factory=lambda: int(os.getenv("OPN_FEE_BPS", "0")))
    kalshi_fee_bps: int = field(default_factory=lambda: int(os.getenv("KALSHI_FEE_BPS", "0")))
    sxbet_fee_bps: int = field(default_factory=lambda: int(os.getenv("SXBET_FEE_BPS", "0")))
    max_cross_date_gap_days: int = 45
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    telegram_min_profit_pct: float = field(default_factory=lambda: float(os.getenv("TELEGRAM_MIN_PROFIT_PCT", "2.0")))
    telegram_statuses: tuple[str, ...] = field(
        default_factory=lambda: _env_list("TELEGRAM_STATUSES", "eligible")
    )
    telegram_cooldown_seconds: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_COOLDOWN_SECONDS", "1800")))
    telegram_log_lines: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_LOG_LINES", "40")))
    telegram_poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_POLL_INTERVAL_SECONDS", "5")))
    telegram_poll_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "10")))
    telegram_health_enabled: bool = field(default_factory=lambda: _env_bool("TELEGRAM_HEALTH_ENABLED", "true"))
    telegram_health_cooldown_seconds: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_HEALTH_COOLDOWN_SECONDS", "1800"))
    )
    telegram_debug_markets_enabled: bool = field(default_factory=lambda: _env_bool("TELEGRAM_DEBUG_MARKETS_ENABLED"))
    telegram_debug_matches_enabled: bool = field(default_factory=lambda: _env_bool("TELEGRAM_DEBUG_MATCHES_ENABLED"))
    telegram_debug_include_empty_matches: bool = field(
        default_factory=lambda: _env_bool("TELEGRAM_DEBUG_INCLUDE_EMPTY_MATCHES", "true")
    )
    telegram_debug_markets_per_exchange: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_DEBUG_MARKETS_PER_EXCHANGE", "5"))
    )
    telegram_debug_batch_size: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_DEBUG_BATCH_SIZE", "5")))
    telegram_debug_match_limit: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_DEBUG_MATCH_LIMIT", "5")))
    telegram_debug_exchanges: tuple[str, ...] = field(default_factory=lambda: _env_list("TELEGRAM_DEBUG_EXCHANGES"))
    notes: list[str] = field(
        default_factory=lambda: [
            "Scanner-first system. Live execution adapters are intentionally gated.",
            "Generic cache mode locks matched pairs and only rematches unmatched markets on future discovery cycles.",
        ]
    )
