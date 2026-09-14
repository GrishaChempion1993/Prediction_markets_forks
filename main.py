from __future__ import annotations

from argparse import ArgumentParser
import os
from pathlib import Path
from time import sleep

from arb_engine.collect_bridge import collect_exchange
from arb_engine.config import Settings
from arb_engine.executor import Executor
from arb_engine.io import latest_snapshot_files, read_snapshot_file
from arb_engine.market_pipeline import MarketPipeline
from arb_engine.matcher import MarketMatcher
from arb_engine.normalize import normalize_snapshots
from arb_engine.scanner import OpportunityScanner
from arb_engine.sports_pipeline import SportsPipeline
from arb_engine.telegram import TelegramNotifier
from arb_engine.utils import load_env_file, utc_now


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def parse_args() -> object:
    sports_mode = _env_bool("SPORTS_ONLY", "false")
    default_pipeline = os.getenv("APP_PIPELINE", "sports" if sports_mode else "generic").strip().lower()
    if default_pipeline not in {"generic", "sports", "notifier"}:
        default_pipeline = "sports" if sports_mode else "generic"
    parser = ArgumentParser(description="Prediction market arbitrage scanner")
    parser.add_argument("--mode", choices=("monitor", "dry_run", "live"), required=True)
    parser.add_argument("--pipeline", choices=("generic", "sports", "notifier"), default=default_pipeline)
    parser.add_argument(
        "--loop",
        type=int,
        default=int(os.getenv("APP_LOOP_SECONDS", "120")),
        help="Sleep interval between cycles in seconds.",
    )
    parser.add_argument("--cycles", type=int, default=1, help="Number of cycles to execute.")
    parser.add_argument("--skip-collect", action="store_true", help="Use latest existing ingest files instead of collecting fresh data.")
    parser.add_argument(
        "--exchanges",
        default=os.getenv("APP_EXCHANGES", "polymarket,opn,azuro,sxbet,myriad,kalshi"),
        help="Comma-separated exchange list.",
    )
    parser.add_argument("--quote-size-usd", type=float, default=100.0)
    parser.add_argument("--min-profit-pct", type=float, default=2.0)
    parser.add_argument("--listen-telegram", action="store_true", default=_env_bool("TELEGRAM_LISTENER_AUTOSTART", "false"))
    parser.add_argument(
        "--max-markets",
        type=int,
        default=int(os.getenv("DISCOVERY_SAFETY_CAP_PER_VENUE", "10000")) if sports_mode else int(os.getenv("APP_MAX_MARKETS", "50")),
    )
    parser.add_argument("--sports-only", action="store_true", default=sports_mode)
    return parser.parse_args()


def run_cycle(
    settings: Settings,
    mode: str,
    exchanges: list[str],
    skip_collect: bool,
    telegram: TelegramNotifier | None = None,
) -> None:
    ingest_root = settings.repo_root / settings.ingest_root
    if skip_collect:
        ingest_files = latest_snapshot_files(ingest_root, exchanges)
    else:
        ingest_files = []
        for exchange in exchanges:
            out_path = ingest_root / exchange / f"{utc_now().isoformat().replace(':', '-')}.jsonl.gz"
            try:
                collect_exchange(settings.repo_root, exchange, out_path, settings.max_markets_per_exchange)
            except Exception as error:  # pragma: no cover - runtime fallback
                print(f"collector_failed exchange={exchange} error={error}")
                continue
            ingest_files.append(out_path)

    snapshots = []
    for path in ingest_files:
        snapshots.extend(read_snapshot_file(path))
    if not snapshots:
        print("No snapshots available.")
        return

    markets = normalize_snapshots(snapshots, settings)
    matcher = MarketMatcher(settings)
    matched_pairs, canonical_events = matcher.match(markets)
    scanner = OpportunityScanner(settings)
    opportunities = scanner.scan(matched_pairs)
    executor = Executor(settings, mode, telegram=telegram)
    executor.persist_cycle(markets, matched_pairs, canonical_events, opportunities)

    print(
        {
            "markets": len(markets),
            "matched_pairs": len(matched_pairs),
            "canonical_events": len(canonical_events),
            "opportunities": len(opportunities),
            "eligible": sum(1 for item in opportunities if item.status == "eligible"),
        }
    )


def main() -> None:
    load_env_file(Path(__file__).resolve().parent / ".env")
    args = parse_args()
    exchanges = [value.strip() for value in args.exchanges.split(",") if value.strip()]
    settings = Settings(
        sports_only=args.pipeline == "sports" or args.sports_only,
        quote_size_usd=args.quote_size_usd,
        min_profit_pct=args.min_profit_pct,
        max_markets_per_exchange=args.max_markets,
    )
    telegram = TelegramNotifier(settings)
    if args.listen_telegram or args.pipeline == "notifier":
        telegram.start_listener()

    if args.pipeline == "notifier":
        remaining_cycles = args.cycles
        while remaining_cycles == 0 or remaining_cycles > 0:
            sleep(max(1, args.loop))
            if remaining_cycles > 0:
                remaining_cycles -= 1
                if remaining_cycles == 0:
                    break
        return

    if args.pipeline == "sports" or settings.sports_only:
        pipeline = SportsPipeline(settings, args.mode, exchanges, telegram=telegram)
        try:
            pipeline.run(args.cycles)
        finally:
            pipeline.close()
        return

    pipeline = MarketPipeline(settings, args.mode, exchanges, telegram=telegram)
    try:
        pipeline.run(args.cycles, args.loop)
    finally:
        pipeline.close()
    return


if __name__ == "__main__":
    main()
