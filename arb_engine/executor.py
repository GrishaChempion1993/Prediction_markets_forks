from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .config import Settings
from .io import write_jsonl
from .models import ArbitrageOpportunity, CanonicalEvent, MatchedPair, NormalizedMarket
from .telegram import TelegramNotifier
from .utils import json_ready, utc_now


class Executor:
    def __init__(self, settings: Settings, mode: str, telegram: TelegramNotifier | None = None) -> None:
        self.settings = settings
        self.mode = mode
        self.telegram = telegram or TelegramNotifier(settings)

    def persist_cycle(
        self,
        markets: list[NormalizedMarket],
        matched_pairs: list[MatchedPair],
        canonical_events: list[CanonicalEvent],
        opportunities: list[ArbitrageOpportunity],
        exchange_statuses: dict[str, dict[str, object]] | None = None,
    ) -> None:
        logs_root = self.settings.repo_root / self.settings.logs_root
        loggable_opportunities = [
            opportunity for opportunity in opportunities if opportunity.status in {"eligible", "review_only"}
        ]
        write_jsonl(logs_root / "matches.jsonl", [self._match_row(pair) for pair in matched_pairs])
        write_jsonl(logs_root / "canonical_events.jsonl", [json_ready(asdict(event)) for event in canonical_events])
        write_jsonl(logs_root / "opportunities.jsonl", [self._opportunity_row(opportunity) for opportunity in loggable_opportunities])
        if exchange_statuses:
            write_jsonl(logs_root / "health.jsonl", [self._health_row(exchange_statuses)])

        if self.mode == "dry_run":
            write_jsonl(
                logs_root / "trades.jsonl",
                [self._dry_run_row(opportunity) for opportunity in opportunities if opportunity.status in {"eligible", "review_only"}],
            )
        elif self.mode == "live":
            write_jsonl(logs_root / "live_intents.jsonl", [self._live_intent_row(opportunity) for opportunity in opportunities])

        try:
            sent = self.telegram.notify_opportunities(opportunities, self.mode)
        except Exception as error:  # pragma: no cover - network/runtime fallback
            print(f"telegram_notifier_failed error={error}")
        else:
            if sent:
                print(f"telegram_sent={sent}")

        try:
            debug_sent = self.telegram.notify_debug_cycle(markets, matched_pairs, canonical_events, self.mode)
        except Exception as error:  # pragma: no cover - network/runtime fallback
            print(f"telegram_debug_failed error={error}")
        else:
            if debug_sent:
                print(f"telegram_debug_sent={debug_sent}")

        if exchange_statuses:
            try:
                health_sent = self.telegram.notify_health(exchange_statuses, self.mode)
            except Exception as error:  # pragma: no cover - network/runtime fallback
                print(f"telegram_health_failed error={error}")
            else:
                if health_sent:
                    print(f"telegram_health_sent={health_sent}")

    @staticmethod
    def _match_row(pair: MatchedPair) -> dict[str, Any]:
        return {
            "timestamp": utc_now(),
            "market_a": f"{pair.market_a.exchange}:{pair.market_a.market_id}",
            "market_b": f"{pair.market_b.exchange}:{pair.market_b.market_id}",
            "confidence": pair.confidence,
            "match_method": pair.match_method,
            "resolve_compatibility": pair.resolve_compatibility,
            "score_breakdown": pair.score_breakdown,
            "outcome_mapping": pair.outcome_mapping,
        }

    @staticmethod
    def _opportunity_row(opportunity: ArbitrageOpportunity) -> dict[str, Any]:
        return {
            "timestamp": utc_now(),
            "strategy": opportunity.strategy,
            "market_a": f"{opportunity.market_a.exchange}:{opportunity.market_a.market_id}",
            "market_b": f"{opportunity.market_b.exchange}:{opportunity.market_b.market_id}",
            "profit_pct": opportunity.profit_pct,
            "cost": opportunity.cost,
            "gross_payout": opportunity.gross_payout,
            "status": opportunity.status,
            "lifecycle_status": opportunity.lifecycle_status,
            "reason": opportunity.reason,
            "match_confidence": opportunity.match_confidence,
            "quote_age_seconds": opportunity.quote_age_seconds,
            "legs": opportunity.legs,
        }

    @staticmethod
    def _health_row(exchange_statuses: dict[str, dict[str, object]]) -> dict[str, Any]:
        return {
            "timestamp": utc_now(),
            "exchanges": exchange_statuses,
        }

    def _dry_run_row(self, opportunity: ArbitrageOpportunity) -> dict[str, Any]:
        fill_ratio = min(
            1.0,
            min(float(leg.get("max_fill_usd", 0.0)) / self.settings.quote_size_usd for leg in opportunity.legs),
        )
        return {
            "timestamp": utc_now(),
            "strategy": opportunity.strategy,
            "status": "simulated_filled" if fill_ratio >= 1.0 else "partial_fill_risk",
            "fill_ratio": fill_ratio,
            "simulated_cost": opportunity.cost * fill_ratio,
            "simulated_payout": opportunity.gross_payout * fill_ratio,
            "residual_exposure_usd": self.settings.quote_size_usd * (1 - fill_ratio),
            "profit_pct": opportunity.profit_pct,
            "source_status": opportunity.status,
            "legs": opportunity.legs,
        }

    def _live_intent_row(self, opportunity: ArbitrageOpportunity) -> dict[str, Any]:
        if opportunity.status != "eligible":
            blocked_reason = opportunity.reason or "opportunity_not_eligible"
        elif opportunity.match_confidence < self.settings.min_live_match_confidence:
            blocked_reason = "match_confidence_below_live_threshold"
        else:
            blocked_reason = "live_execution_adapters_not_implemented"
        return {
            "timestamp": utc_now(),
            "strategy": opportunity.strategy,
            "eligible_for_submission": False,
            "blocked_reason": blocked_reason,
            "profit_pct": opportunity.profit_pct,
            "match_confidence": opportunity.match_confidence,
            "legs": opportunity.legs,
        }
