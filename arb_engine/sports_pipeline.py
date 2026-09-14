from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import sleep
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

from .collect_bridge import collect_exchange
from .config import Settings
from .io import read_snapshot_file
from .models import MarketSnapshot
from .sports_db import SportsCache
from .sports_matcher import SportsEventMatcher
from .sports_models import SportsCanonicalEvent, SportsMarket, SportsOpportunity
from .sports_normalize import normalize_sports_snapshots
from .sports_scanner import SportsArbitrageScanner
from .telegram import TelegramNotifier
from .utils import utc_now


AZURO_API_BASE = "https://api.onchainfeed.org"
POLYMARKET_CLOB_BASE = "https://clob.polymarket.com"


@dataclass(slots=True)
class DiscoveryResult:
    by_exchange: dict[str, list[SportsMarket]]
    canonical_events: list[SportsCanonicalEvent]
    review_items: list[dict[str, object]]


class SportsPipeline:
    def __init__(self, settings: Settings, mode: str, exchanges: list[str], telegram: TelegramNotifier | None = None) -> None:
        self.settings = settings
        self.mode = mode
        self.exchanges = exchanges
        self.telegram = telegram or TelegramNotifier(settings)
        self.cache = SportsCache(settings)
        self.matcher = SportsEventMatcher(settings)
        self.scanner = SportsArbitrageScanner(settings)
        self.last_discovery = DiscoveryResult(by_exchange={}, canonical_events=[], review_items=[])

    def close(self) -> None:
        self.cache.close()

    def run(self, cycles: int) -> None:
        next_discovery = utc_now()
        next_refresh = utc_now()
        next_cleanup = utc_now() + timedelta(seconds=self.settings.cleanup_interval_seconds)
        next_digest = utc_now()
        scans_completed = 0

        while cycles == 0 or scans_completed < cycles:
            now = utc_now()
            if now >= next_discovery:
                self.last_discovery = self._run_discovery()
                next_discovery = now + timedelta(seconds=self.settings.discovery_interval_seconds)

            if now >= next_refresh:
                refreshed_markets = self._refresh_quotes()
                if refreshed_markets:
                    self.cache.upsert_market_refresh(refreshed_markets)
                opportunities = self.scanner.scan(self.cache.load_canonical_events(linked_only=True))
                alerts = self.cache.upsert_opportunities(opportunities)
                self._notify_alerts(alerts)
                summary = self.cache.live_counts()
                summary.update(
                    {
                        "opportunities": len(opportunities),
                        "eligible": sum(1 for item in opportunities if item.status == "eligible"),
                        "alerts": len(alerts),
                    }
                )
                print(summary)
                scans_completed += 1
                next_refresh = now + timedelta(seconds=self.settings.quote_refresh_interval_seconds)

            if now >= next_digest:
                self._send_sampled_digests()
                next_digest = now + timedelta(seconds=self.settings.sports_digest_interval_seconds)

            if now >= next_cleanup:
                self.cache.cleanup()
                next_cleanup = now + timedelta(seconds=self.settings.cleanup_interval_seconds)

            if cycles != 0 and scans_completed >= cycles:
                break
            sleep(1)

    def _run_discovery(self) -> DiscoveryResult:
        by_exchange: dict[str, list[SportsMarket]] = {}
        discover_cap = min(self.settings.discovery_safety_cap_per_venue, self.settings.max_markets_per_exchange)
        for exchange in self.exchanges:
            out_path = self.settings.repo_root / self.settings.ingest_root / exchange / f"{utc_now().isoformat().replace(':', '-')}.jsonl.gz"
            try:
                env_overrides = {"POLYMARKET_BOOK_LIMIT": "0"} if exchange == "polymarket" else None
                collect_exchange(self.settings.repo_root, exchange, out_path, discover_cap, env_overrides=env_overrides)
                snapshots = read_snapshot_file(out_path)
            except Exception as error:  # pragma: no cover - runtime fallback
                print(f"sports_discovery_failed exchange={exchange} error={error}")
                continue
            sports_markets = normalize_sports_snapshots(snapshots, self.settings)
            self.cache.upsert_discovery(exchange, out_path, snapshots, sports_markets)
            by_exchange[exchange] = sports_markets
            print(f"sports_discovered exchange={exchange} markets={len(sports_markets)}")

        self.matcher = SportsEventMatcher(self.settings, aliases=self.cache.load_aliases())
        canonical_events, event_links, review_items = self.matcher.link(self.cache.load_active_markets())
        self.cache.replace_links(canonical_events, event_links, review_items)
        print(
            {
                "sports_markets": sum(len(items) for items in by_exchange.values()),
                "canonical_events": len(canonical_events),
                "review_items": len(review_items),
            }
        )
        return DiscoveryResult(by_exchange=by_exchange, canonical_events=canonical_events, review_items=review_items)

    def _refresh_quotes(self) -> list[SportsMarket]:
        targets = self.cache.load_quote_refresh_targets()
        grouped: dict[str, list[SportsMarket]] = defaultdict(list)
        for market in targets:
            grouped[market.exchange].append(market)

        refreshed: list[SportsMarket] = []
        if grouped.get("polymarket"):
            refreshed.extend(self._refresh_polymarket(grouped["polymarket"]))
        if grouped.get("azuro"):
            refreshed.extend(self._refresh_azuro(grouped["azuro"]))
        if grouped.get("opn"):
            refreshed.extend(self._refresh_opn(grouped["opn"]))
        if grouped.get("kalshi"):
            refreshed.extend(self._refresh_kalshi(grouped["kalshi"]))
        if grouped.get("myriad"):
            refreshed.extend(self._refresh_myriad(grouped["myriad"]))
        return refreshed

    def _refresh_polymarket(self, markets: list[SportsMarket]) -> list[SportsMarket]:
        snapshots: list[MarketSnapshot] = []
        for market in markets:
            raw = dict(market.raw_data)
            clob_token_ids = raw.get("clobTokenIds") if isinstance(raw.get("clobTokenIds"), list) else []
            order_books: dict[str, object] = {}
            for token_id in clob_token_ids:
                try:
                    order_books[str(token_id)] = _fetch_json(f"{POLYMARKET_CLOB_BASE}/book?token_id={token_id}")
                except Exception as error:  # pragma: no cover - runtime fallback
                    order_books[str(token_id)] = {"error": str(error)}
            raw["orderBooks"] = order_books
            snapshots.append(
                MarketSnapshot(
                    schema_version="market_snapshot.v1",
                    exchange="polymarket",
                    market_id=market.market_id,
                    fetched_at=utc_now(),
                    source={"collector": "py.sports_refresh.polymarket", "endpoints": [f"{POLYMARKET_CLOB_BASE}/book"]},
                    raw=raw,
                )
            )
        return normalize_sports_snapshots(snapshots, self.settings)

    def _refresh_azuro(self, markets: list[SportsMarket]) -> list[SportsMarket]:
        grouped: dict[str, dict[str, SportsMarket]] = defaultdict(dict)
        for market in markets:
            environment = str(market.raw_data.get("environment") or "unknown")
            game_id = str(market.raw_data.get("game", {}).get("gameId") or "")
            if game_id:
                grouped[environment][game_id] = market

        refreshed_snapshots: list[MarketSnapshot] = []
        for environment, game_map in grouped.items():
            game_ids = list(game_map)
            for index in range(0, len(game_ids), 10):
                chunk = game_ids[index : index + 10]
                payload = {"environment": environment, "gameIds": chunk}
                response = _post_json(f"{AZURO_API_BASE}/api/v1/public/market-manager/conditions-by-game-ids", payload)
                for condition in response.get("conditions", []):
                    game_id = str(condition.get("game", {}).get("gameId") or "")
                    market = game_map.get(game_id)
                    if market is None:
                        continue
                    raw = dict(market.raw_data)
                    raw["condition"] = condition
                    refreshed_snapshots.append(
                        MarketSnapshot(
                            schema_version="market_snapshot.v1",
                            exchange="azuro",
                            market_id=str(condition.get("conditionId") or condition.get("id") or market.market_id),
                            fetched_at=utc_now(),
                            source={"collector": "py.sports_refresh.azuro", "endpoints": [f"{AZURO_API_BASE}/api/v1/public/market-manager/conditions-by-game-ids"]},
                            raw=raw,
                        )
                    )
        return normalize_sports_snapshots(refreshed_snapshots, self.settings)

    def _refresh_myriad(self, markets: list[SportsMarket]) -> list[SportsMarket]:
        out_path = self.settings.repo_root / "tmp" / f"myriad-refresh-{utc_now().isoformat().replace(':', '-')}.jsonl.gz"
        try:
            collect_exchange(self.settings.repo_root, "myriad", out_path, self.settings.discovery_safety_cap_per_venue)
            snapshots = read_snapshot_file(out_path)
        except Exception as error:  # pragma: no cover - runtime fallback
            print(f"myriad_refresh_failed error={error}")
            return []
        wanted_ids = {market.market_id for market in markets}
        filtered = [snapshot for snapshot in snapshots if snapshot.market_id in wanted_ids]
        return normalize_sports_snapshots(filtered, self.settings)

    def _refresh_opn(self, markets: list[SportsMarket]) -> list[SportsMarket]:
        out_path = self.settings.repo_root / "tmp" / f"opn-refresh-{utc_now().isoformat().replace(':', '-')}.jsonl.gz"
        try:
            collect_exchange(self.settings.repo_root, "opn", out_path, self.settings.discovery_safety_cap_per_venue)
            snapshots = read_snapshot_file(out_path)
        except Exception as error:  # pragma: no cover - runtime fallback
            print(f"opn_refresh_failed error={error}")
            return []
        wanted_ids = {market.market_id for market in markets}
        filtered = [snapshot for snapshot in snapshots if snapshot.market_id in wanted_ids]
        return normalize_sports_snapshots(filtered, self.settings)

    def _refresh_kalshi(self, markets: list[SportsMarket]) -> list[SportsMarket]:
        snapshots: list[MarketSnapshot] = []
        for market in markets:
            ticker = str(market.raw_data.get("ticker") or market.market_id)
            try:
                payload = _fetch_json(f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}")
                raw = dict(payload.get("market") or {})
            except Exception as error:  # pragma: no cover - runtime fallback
                print(f"kalshi_refresh_failed market={ticker} error={error}")
                continue
            snapshots.append(
                MarketSnapshot(
                    schema_version="market_snapshot.v1",
                    exchange="kalshi",
                    market_id=ticker,
                    fetched_at=utc_now(),
                    source={"collector": "py.sports_refresh.kalshi", "endpoints": ["https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"]},
                    raw=raw,
                )
            )
        return normalize_sports_snapshots(snapshots, self.settings)

    def _notify_alerts(self, alerts: list[SportsOpportunity]) -> None:
        if not self.telegram.enabled:
            return
        for opportunity in alerts:
            self.telegram.send_text(self._format_opportunity_message(opportunity))

    def _send_sampled_digests(self) -> None:
        if not self.telegram.enabled:
            return

        counts = self.cache.counts()
        summary_lines = [
            "[SPORTS][SUMMARY]",
            f"active_markets={counts['venue_markets']}",
            f"canonical_events={counts['canonical_events']}",
            f"linked_events={counts['linked_events']}",
            f"review_items={counts['review_items']}",
        ]
        self.telegram.send_text("\n".join(summary_lines))

        limit = max(1, self.settings.sports_digest_market_limit)
        for exchange, markets in self.last_discovery.by_exchange.items():
            ranked = sorted(markets, key=lambda item: (item.volume_usd, item.liquidity_usd), reverse=True)[:limit]
            if not ranked:
                continue
            lines = [f"[SPORTS][{exchange.upper()}][TOP {len(ranked)}]"]
            for index, market in enumerate(ranked, start=1):
                start_time = market.start_time.isoformat()[:16] if market.start_time else "n/a"
                lines.append(f"{index}. {market.title}")
                lines.append(f"   {market.market_family} | {market.sport_key} | start={start_time} | vol=${market.volume_usd:.0f}")
            self.telegram.send_text("\n".join(lines))

        linked = [event for event in self.last_discovery.canonical_events if len({market.exchange for market in event.markets}) >= 2]
        linked = sorted(linked, key=lambda item: (len({market.exchange for market in item.markets}), len(item.markets)), reverse=True)
        if linked:
            lines = [f"[SPORTS][LINKS][TOP {min(len(linked), self.settings.sports_digest_match_limit)}]"]
            for index, event in enumerate(linked[: self.settings.sports_digest_match_limit], start=1):
                exchanges = ",".join(sorted({market.exchange for market in event.markets}))
                start_time = event.start_time.isoformat()[:16] if event.start_time else "n/a"
                lines.append(f"{index}. {event.title}")
                lines.append(f"   lane={event.match_lane} | sport={event.sport_key} | exchanges={exchanges} | start={start_time}")
            self.telegram.send_text("\n".join(lines))

        review_count = len(self.last_discovery.review_items)
        if review_count:
            lines = [f"[SPORTS][REVIEW] count={review_count}"]
            for item in self.last_discovery.review_items[: self.settings.sports_digest_match_limit]:
                payload = item.get("payload_json", {})
                lines.append(f"- {payload.get('title_a', 'n/a')} <-> {payload.get('title_b', 'n/a')} | conf={float(item.get('confidence', 0.0)):.2f}")
            self.telegram.send_text("\n".join(lines))

    @staticmethod
    def _format_opportunity_message(opportunity: SportsOpportunity) -> str:
        lines = [
            "[SPORTS][ARBITRAGE]",
            f"strategy={opportunity.strategy}",
            f"profit={opportunity.profit_pct:.2f}%",
            f"status={opportunity.status}",
            f"A={opportunity.market_a.exchange} | {opportunity.market_a.title}",
            f"B={opportunity.market_b.exchange} | {opportunity.market_b.title}",
            "legs:",
        ]
        for leg in opportunity.legs:
            lines.append(
                f"- {leg.get('exchange')} buy {leg.get('buy_outcome')} "
                f"cost={float(leg.get('quoted_cost') or 0.0):.2f} ask={float(leg.get('best_ask') or 0.0):.4f}"
            )
        lines.append(f"total_cost={opportunity.cost:.2f} payout={opportunity.gross_payout:.2f}")
        if opportunity.reason:
            lines.append(f"reason={opportunity.reason}")
        return "\n".join(lines)


def _fetch_json(url: str, method: str = "GET", payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None
    headers = {"user-agent": "prediction-arb-scanner/0.1"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    request = Request(url=url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    return _fetch_json(url, method="POST", payload=payload)
