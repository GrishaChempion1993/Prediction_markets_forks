from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import OutcomeQuote


@dataclass(slots=True)
class SportsOutcome:
    outcome_id: str
    label: str
    canonical_label: str
    selection_key: str
    quote: OutcomeQuote


@dataclass(slots=True)
class SportsMarket:
    exchange: str
    market_id: str
    title: str
    description: str
    event_class: str
    match_lane: str
    sport_key: str
    competition_key: str
    season_key: str | None
    is_esports: bool
    participants_home: str | None
    participants_away: str | None
    participants_sorted: tuple[str, ...]
    start_time: datetime | None
    best_of: int | None
    period_scope: str
    map_no: int | None
    round_no: int | None
    market_family: str
    line_value: float | None
    line_side: str | None
    auto_arb_enabled: bool
    chain: str
    token: str
    outcomes: list[SportsOutcome]
    volume_usd: float
    liquidity_usd: float
    fetched_at: datetime
    raw_data: dict[str, Any]
    resolution_profile: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SportsLink:
    market_a: SportsMarket
    market_b: SportsMarket
    confidence: float
    status: str
    match_lane: str
    score_breakdown: dict[str, float]
    veto_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SportsCanonicalEvent:
    canonical_event_id: str
    title: str
    event_class: str
    match_lane: str
    sport_key: str
    competition_key: str
    season_key: str | None
    is_esports: bool
    participants_sorted: tuple[str, ...]
    start_time: datetime | None
    best_of: int | None
    markets: list[SportsMarket]


@dataclass(slots=True)
class SportsOpportunity:
    opportunity_key: str
    strategy: str
    canonical_event_id: str
    market_a: SportsMarket
    market_b: SportsMarket
    legs: list[dict[str, Any]]
    cost: float
    gross_payout: float
    profit_pct: float
    match_confidence: float
    status: str
    quote_age_seconds: float
    detected_at: datetime
    lifecycle_status: str = "live"
    reason: str | None = None
