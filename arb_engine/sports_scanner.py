from __future__ import annotations

from datetime import UTC, datetime
from itertools import combinations

from .config import Settings
from .resolve import resolution_profile_signature
from .sports_models import SportsCanonicalEvent, SportsMarket, SportsOpportunity, SportsOutcome
from .utils import text_hash


class SportsArbitrageScanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def scan(self, canonical_events: list[SportsCanonicalEvent]) -> list[SportsOpportunity]:
        opportunities: list[SportsOpportunity] = []
        now = datetime.now(tz=UTC)
        for event in canonical_events:
            variants = self._group_variants(event.markets)
            for variant_markets in variants.values():
                for left_market, right_market in combinations(variant_markets, 2):
                    if left_market.exchange == right_market.exchange:
                        continue
                    opportunity = self._scan_market_pair(event.canonical_event_id, left_market, right_market, now)
                    if opportunity:
                        opportunities.append(opportunity)
        return sorted(opportunities, key=lambda item: item.profit_pct, reverse=True)

    def _group_variants(self, markets: list[SportsMarket]) -> dict[str, list[SportsMarket]]:
        grouped: dict[str, list[SportsMarket]] = {}
        for market in markets:
            if not market.auto_arb_enabled:
                continue
            if market.market_family not in self.settings.sports_auto_arb_families:
                continue
            grouped.setdefault(self._variant_key(market), []).append(market)
        return grouped

    def _scan_market_pair(
        self,
        canonical_event_id: str,
        left_market: SportsMarket,
        right_market: SportsMarket,
        now: datetime,
    ) -> SportsOpportunity | None:
        left_outcomes = {outcome.selection_key: outcome for outcome in left_market.outcomes}
        right_outcomes = {outcome.selection_key: outcome for outcome in right_market.outcomes}
        if set(left_outcomes) != set(right_outcomes):
            return None

        legs: list[dict[str, object]] = []
        cost = 0.0
        max_age = 0.0
        for selection_key in sorted(left_outcomes):
            chosen_market, chosen_outcome = self._best_outcome(left_market, left_outcomes[selection_key], right_market, right_outcomes[selection_key])
            if chosen_outcome.quote.quoted_cost_for_size is None:
                return None
            quote_age = max((now - chosen_outcome.quote.quote_timestamp).total_seconds(), 0.0)
            max_age = max(max_age, quote_age)
            cost += chosen_outcome.quote.quoted_cost_for_size
            legs.append(
                {
                    "exchange": chosen_market.exchange,
                    "market_id": chosen_market.market_id,
                    "selection_key": selection_key,
                    "buy_outcome": chosen_outcome.label,
                    "quoted_cost": chosen_outcome.quote.quoted_cost_for_size,
                    "best_ask": chosen_outcome.quote.best_ask,
                    "max_fill_usd": chosen_outcome.quote.max_fill_usd,
                }
            )

        if cost <= 0:
            return None
        payout = self.settings.quote_size_usd
        profit_pct = ((payout - cost) / cost) * 100
        status = "eligible"
        reason = None
        if max_age > self.settings.quote_ttl_seconds:
            status = "rejected"
            reason = "stale_quote"
        elif any(float(leg.get("max_fill_usd", 0.0)) < self.settings.quote_size_usd for leg in legs):
            status = "rejected"
            reason = "insufficient_depth"
        elif profit_pct < self.settings.min_profit_pct:
            status = "rejected"
            reason = "profit_below_threshold"

        opportunity_key = text_hash(
            "|".join(
                [
                    canonical_event_id,
                    self._variant_key(left_market),
                    left_market.exchange,
                    right_market.exchange,
                    ",".join(leg["selection_key"] for leg in legs),
                ]
            )
        )
        return SportsOpportunity(
            opportunity_key=opportunity_key,
            strategy=f"sports_cover_{left_market.market_family}",
            canonical_event_id=canonical_event_id,
            market_a=left_market,
            market_b=right_market,
            legs=legs,
            cost=cost,
            gross_payout=payout,
            profit_pct=profit_pct,
            match_confidence=1.0,
            status=status,
            quote_age_seconds=max_age,
            detected_at=now,
            reason=reason,
        )

    @staticmethod
    def _best_outcome(
        left_market: SportsMarket,
        left_outcome: SportsOutcome,
        right_market: SportsMarket,
        right_outcome: SportsOutcome,
    ) -> tuple[SportsMarket, SportsOutcome]:
        left_cost = left_outcome.quote.quoted_cost_for_size if left_outcome.quote.quoted_cost_for_size is not None else float("inf")
        right_cost = right_outcome.quote.quoted_cost_for_size if right_outcome.quote.quoted_cost_for_size is not None else float("inf")
        if left_cost <= right_cost:
            return left_market, left_outcome
        return right_market, right_outcome

    @staticmethod
    def _variant_key(market: SportsMarket) -> str:
        outcome_shape = "|".join(sorted(outcome.selection_key for outcome in market.outcomes))
        line_value = f"{market.line_value:g}" if market.line_value is not None else "na"
        return "|".join(
            [
                resolution_profile_signature(market.resolution_profile),
                market.market_family,
                market.period_scope,
                str(market.map_no or 0),
                str(market.round_no or 0),
                line_value,
                market.line_side or "na",
                outcome_shape,
            ]
        )
