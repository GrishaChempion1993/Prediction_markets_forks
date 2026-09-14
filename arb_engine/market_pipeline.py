from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import sleep

from .collect_bridge import collect_exchange
from .config import Settings
from .executor import Executor
from .io import read_snapshot_file
from .market_db import MarketCache
from .matcher import MarketMatcher
from .normalize import normalize_snapshots
from .scanner import OpportunityScanner
from .telegram import TelegramNotifier
from .utils import utc_now


@dataclass(slots=True)
class DiscoveryBatch:
    markets: list
    new_pairs: list
    opportunities: list
    exchange_statuses: dict[str, dict[str, object]]


class MarketPipeline:
    def __init__(self, settings: Settings, mode: str, exchanges: list[str], telegram: TelegramNotifier | None = None) -> None:
        self.settings = settings
        self.mode = mode
        self.exchanges = exchanges
        self.telegram = telegram or TelegramNotifier(settings)
        self.cache = MarketCache(settings)
        self.matcher = MarketMatcher(settings)
        self.scanner = OpportunityScanner(settings)
        self.executor = Executor(settings, mode, telegram=self.telegram)

    def close(self) -> None:
        self.cache.close()

    def run(self, cycles: int, loop_seconds: int) -> None:
        cycle = 0
        while cycles == 0 or cycle < cycles:
            batch = self._run_discovery_and_scan()
            summary = self.cache.live_counts()
            summary.update(
                {
                    "discovered_markets": len(batch.markets),
                    "new_pairs": len(batch.new_pairs),
                    "eligible": sum(1 for item in batch.opportunities if item.status == "eligible"),
                }
            )
            print(summary)
            cycle += 1
            if cycles == 0 or cycle < cycles:
                sleep(loop_seconds)

    def _run_discovery_and_scan(self) -> DiscoveryBatch:
        discovered_markets = []
        frontier_markets = []
        exchange_statuses: dict[str, dict[str, object]] = {}
        for exchange in self.exchanges:
            out_path = self.settings.repo_root / self.settings.ingest_root / exchange / f"{utc_now().isoformat().replace(':', '-')}.jsonl.gz"
            try:
                collect_exchange(self.settings.repo_root, exchange, out_path, self.settings.max_markets_per_exchange)
                snapshots = read_snapshot_file(out_path)
                normalized = normalize_snapshots(snapshots, self.settings)
                frontier_markets.extend(self.cache.upsert_discovery(exchange, out_path, snapshots, normalized))
                discovered_markets.extend(normalized)
                print(f"discovered exchange={exchange} markets={len(normalized)}")
                exchange_statuses[exchange] = self._exchange_status(exchange, out_path, snapshots, normalized)
            except Exception as error:  # pragma: no cover - runtime fallback
                print(f"discovery_failed exchange={exchange} error={error}")
                exchange_statuses[exchange] = {
                    "status": "error",
                    "market_count": 0,
                    "snapshot_path": out_path.as_posix(),
                    "error": str(error),
                }

        self.cache.refresh_lifecycle()
        active_markets = self.cache.load_active_markets()
        new_pairs, _ = self.matcher.match_frontier(frontier_markets, active_markets) if frontier_markets else ([], [])
        stored_pairs = self.cache.store_pairs(new_pairs)
        self.cache.refresh_lifecycle()
        monitored_pairs = self.cache.load_pairs()
        opportunities = self.scanner.scan(monitored_pairs)
        self.cache.upsert_opportunities(opportunities)
        self.cache.cleanup()
        self.executor.persist_cycle(
            discovered_markets,
            stored_pairs,
            [],
            opportunities,
            exchange_statuses=exchange_statuses,
        )
        return DiscoveryBatch(
            markets=discovered_markets,
            new_pairs=stored_pairs,
            opportunities=opportunities,
            exchange_statuses=exchange_statuses,
        )

    @staticmethod
    def _exchange_status(exchange: str, out_path: Path, snapshots: list, normalized: list) -> dict[str, object]:
        sources = [dict(snapshot.source) for snapshot in snapshots]
        source_statuses = [str(source.get("status") or "ok") for source in sources]
        if "error" in source_statuses:
            status = "error"
        elif "degraded" in source_statuses:
            status = "degraded"
        elif "partial" in source_statuses:
            status = "partial"
        else:
            status = "ok" if normalized else "empty"
        completeness_values: list[float] = []
        variants: set[str] = set()
        notes: list[str] = []
        for source in sources:
            variants.add(str(source.get("variant") or "default"))
            if source.get("completeness") is not None:
                try:
                    completeness_values.append(float(source["completeness"]))
                except (TypeError, ValueError):
                    pass
            notes.extend(str(note) for note in source.get("notes", []))
        return {
            "status": status,
            "market_count": len(normalized),
            "snapshot_path": out_path.as_posix(),
            "variant": ",".join(sorted(variants)) if variants else "default",
            "completeness": min(completeness_values) if completeness_values else 1.0,
            "notes": sorted(dict.fromkeys(notes))[:10],
        }
