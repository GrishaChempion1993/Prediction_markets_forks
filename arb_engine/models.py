from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class MarketSnapshot:
    schema_version: str
    exchange: str
    market_id: str
    fetched_at: datetime
    source: dict[str, Any]
    raw: dict[str, Any]


@dataclass(slots=True)
class OutcomeQuote:
    outcome_id: str
    label: str
    quote_type: str
    best_bid: float | None
    best_ask: float | None
    quoted_cost_for_size: float | None
    fee_bps: int
    slippage_bps_est: int
    max_fill_usd: float
    quote_timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedOutcome:
    outcome_id: str
    label: str
    canonical_label: str
    quote: OutcomeQuote


@dataclass(slots=True)
class NormalizedMarket:
    exchange: str
    market_id: str
    market_type: str
    event_kind: str
    title: str
    description: str
    category: str
    category_family: str
    tags: list[str]
    chain: str
    token: str
    closes_at: datetime | None
    resolves_at: datetime | None
    resolution_source_url: str | None
    resolution_rules_hash: str
    outcomes: list[NormalizedOutcome]
    volume_usd: float
    liquidity_usd: float
    fetched_at: datetime
    raw_data: dict[str, Any]
    resolution_profile: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CanonicalEvent:
    event_id: str
    title: str
    event_kind: str
    entities: list[str]
    numbers: list[str]
    closes_at: datetime | None
    resolution_rules_hashes: list[str]
    markets: list[NormalizedMarket]


@dataclass(slots=True)
class MatchedPair:
    market_a: NormalizedMarket
    market_b: NormalizedMarket
    confidence: float
    match_method: str
    outcome_mapping: dict[str, str]
    score_breakdown: dict[str, float]
    resolve_compatibility: str = "compatible"
    veto_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ArbitrageOpportunity:
    strategy: str
    market_a: NormalizedMarket
    market_b: NormalizedMarket
    legs: list[dict[str, Any]]
    cost: float
    gross_payout: float
    profit_pct: float
    match_confidence: float
    status: str
    quote_age_seconds: float
    lifecycle_status: str = "live"
    reason: str | None = None
