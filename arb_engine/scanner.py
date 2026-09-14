from __future__ import annotations

from datetime import UTC, datetime

from .config import Settings
from .models import ArbitrageOpportunity, MatchedPair, NormalizedMarket, NormalizedOutcome


def _quote_age_seconds(timestamp: datetime, now: datetime) -> float:
    return max((now - timestamp).total_seconds(), 0.0)


class OpportunityScanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def scan(self, matched_pairs: list[MatchedPair]) -> list[ArbitrageOpportunity]:
        opportunities: list[ArbitrageOpportunity] = []
        now = datetime.now(tz=UTC)
        for pair in matched_pairs:
            if pair.market_a.market_type == "binary" and pair.market_b.market_type == "binary":
                opportunities.extend(self._scan_binary(pair, now))
            elif pair.market_a.market_type == "multi_outcome" and pair.market_b.market_type == "multi_outcome":
                opportunity = self._scan_multi(pair, now)
                if opportunity:
                    opportunities.append(opportunity)
        return sorted(opportunities, key=lambda item: item.profit_pct, reverse=True)

    def _scan_binary(self, pair: MatchedPair, now: datetime) -> list[ArbitrageOpportunity]:
        if len(pair.market_a.outcomes) != 2 or len(pair.market_b.outcomes) != 2:
            return []

        mapping = pair.outcome_mapping
        labels_a = list(mapping.keys())
        if len(labels_a) != 2:
            return []

        opportunities: list[ArbitrageOpportunity] = []
        for selected_label in labels_a:
            complementary_label = next(label for label in labels_a if label != selected_label)
            left_outcome = self._find_outcome(pair.market_a, selected_label)
            right_outcome = self._find_outcome(pair.market_b, mapping[complementary_label])
            if left_outcome is None or right_outcome is None:
                continue
            opportunity = self._build_binary_opportunity(pair, left_outcome, right_outcome, now)
            if opportunity:
                opportunities.append(opportunity)
        return opportunities

    def _build_binary_opportunity(
        self,
        pair: MatchedPair,
        left_outcome: NormalizedOutcome,
        right_outcome: NormalizedOutcome,
        now: datetime,
    ) -> ArbitrageOpportunity | None:
        left_quote = left_outcome.quote
        right_quote = right_outcome.quote
        quote_age = max(_quote_age_seconds(left_quote.quote_timestamp, now), _quote_age_seconds(right_quote.quote_timestamp, now))
        status = "eligible"
        reason = None
        if quote_age > self.settings.quote_ttl_seconds:
            status = "rejected"
            reason = "stale_quote"
        if left_quote.quoted_cost_for_size is None or right_quote.quoted_cost_for_size is None:
            status = "rejected"
            reason = "missing_quote"
        if left_quote.max_fill_usd < self.settings.quote_size_usd or right_quote.max_fill_usd < self.settings.quote_size_usd:
            status = "rejected"
            reason = "insufficient_depth"
        if not self._is_auto_tradeable(pair.market_a) or not self._is_auto_tradeable(pair.market_b):
            status = "review_only"
            reason = "market_requires_review"

        if left_quote.quoted_cost_for_size is None or right_quote.quoted_cost_for_size is None:
            return None

        cost = left_quote.quoted_cost_for_size + right_quote.quoted_cost_for_size
        if cost <= 0:
            return None
        payout = self.settings.quote_size_usd
        profit_pct = ((payout - cost) / cost) * 100
        if profit_pct < self.settings.min_profit_pct and status == "eligible":
            status = "rejected"
            reason = "profit_below_threshold"

        return ArbitrageOpportunity(
            strategy="binary_cross_exchange",
            market_a=pair.market_a,
            market_b=pair.market_b,
            legs=[
                {
                    "exchange": pair.market_a.exchange,
                    "market_id": pair.market_a.market_id,
                    "buy_outcome": left_outcome.label,
                    "quoted_cost": left_quote.quoted_cost_for_size,
                    "best_ask": left_quote.best_ask,
                    "max_fill_usd": left_quote.max_fill_usd,
                },
                {
                    "exchange": pair.market_b.exchange,
                    "market_id": pair.market_b.market_id,
                    "buy_outcome": right_outcome.label,
                    "quoted_cost": right_quote.quoted_cost_for_size,
                    "best_ask": right_quote.best_ask,
                    "max_fill_usd": right_quote.max_fill_usd,
                },
            ],
            cost=cost,
            gross_payout=payout,
            profit_pct=profit_pct,
            match_confidence=pair.confidence,
            status=status,
            quote_age_seconds=quote_age,
            reason=reason,
        )

    def _scan_multi(self, pair: MatchedPair, now: datetime) -> ArbitrageOpportunity | None:
        mapping = pair.outcome_mapping
        if len(mapping) != len(pair.market_a.outcomes):
            return None

        canonical_other = {"other", "misc"}
        if any(outcome.canonical_label in canonical_other for outcome in pair.market_a.outcomes + pair.market_b.outcomes):
            status = "review_only"
            reason = "other_bucket_present"
        else:
            status = "eligible" if pair.match_method == "manual" or pair.confidence >= self.settings.min_match_confidence else "review_only"
            reason = None if status == "eligible" else "needs_manual_review"

        legs: list[dict[str, object]] = []
        total_cost = 0.0
        max_age = 0.0

        for left_label, right_label in mapping.items():
            left_outcome = self._find_outcome(pair.market_a, left_label)
            right_outcome = self._find_outcome(pair.market_b, right_label)
            if left_outcome is None or right_outcome is None:
                return None
            candidates = [left_outcome, right_outcome]
            chosen = min(
                candidates,
                key=lambda outcome: outcome.quote.quoted_cost_for_size if outcome.quote.quoted_cost_for_size is not None else float("inf"),
            )
            if chosen.quote.quoted_cost_for_size is None:
                return None
            max_age = max(max_age, _quote_age_seconds(chosen.quote.quote_timestamp, now))
            total_cost += chosen.quote.quoted_cost_for_size
            legs.append(
                {
                    "exchange": pair.market_a.exchange if chosen is left_outcome else pair.market_b.exchange,
                    "market_id": pair.market_a.market_id if chosen is left_outcome else pair.market_b.market_id,
                    "buy_outcome": chosen.label,
                    "quoted_cost": chosen.quote.quoted_cost_for_size,
                    "best_ask": chosen.quote.best_ask,
                    "max_fill_usd": chosen.quote.max_fill_usd,
                }
            )

        if total_cost <= 0:
            return None
        if max_age > self.settings.quote_ttl_seconds:
            status = "rejected"
            reason = "stale_quote"
        profit_pct = ((self.settings.quote_size_usd - total_cost) / total_cost) * 100
        if profit_pct < self.settings.min_profit_pct and status == "eligible":
            status = "rejected"
            reason = "profit_below_threshold"

        return ArbitrageOpportunity(
            strategy="multi_outcome_cover",
            market_a=pair.market_a,
            market_b=pair.market_b,
            legs=legs,
            cost=total_cost,
            gross_payout=self.settings.quote_size_usd,
            profit_pct=profit_pct,
            match_confidence=pair.confidence,
            status=status,
            quote_age_seconds=max_age,
            reason=reason,
        )

    @staticmethod
    def _find_outcome(market: NormalizedMarket, label: str) -> NormalizedOutcome | None:
        for outcome in market.outcomes:
            if outcome.label == label:
                return outcome
        return None

    @staticmethod
    def _is_auto_tradeable(market: NormalizedMarket) -> bool:
        if market.market_type == "binary" and market.category_family != "sports":
            return True
        if market.category_family == "sports":
            return bool(market.features.get("participants")) and bool(market.features.get("has_start_time"))
        if market.market_type == "multi_outcome":
            return not any(outcome.canonical_label in {"other", "misc"} for outcome in market.outcomes)
        return False
