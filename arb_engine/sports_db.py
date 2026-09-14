from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import json
import sqlite3

from .config import Settings
from .models import OutcomeQuote
from .sports_models import SportsCanonicalEvent, SportsMarket, SportsOpportunity, SportsOutcome
from .utils import ensure_parent, json_ready, parse_datetime, text_hash, utc_now


class SportsCache:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.repo_root / settings.sports_db_path
        ensure_parent(self.path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def _init_schema(self) -> None:
        cursor = self.connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS raw_snapshots (
                exchange TEXT NOT NULL,
                market_id TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                source_json TEXT NOT NULL,
                PRIMARY KEY (exchange, market_id, fetched_at)
            );

            CREATE TABLE IF NOT EXISTS venue_markets (
                exchange TEXT NOT NULL,
                market_id TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                title TEXT NOT NULL,
                event_class TEXT NOT NULL,
                match_lane TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                competition_key TEXT NOT NULL,
                season_key TEXT,
                is_esports INTEGER NOT NULL,
                participants_home TEXT,
                participants_away TEXT,
                participants_sorted_json TEXT NOT NULL,
                start_time TEXT,
                best_of INTEGER,
                period_scope TEXT NOT NULL,
                map_no INTEGER,
                round_no INTEGER,
                market_family TEXT NOT NULL,
                line_value REAL,
                line_side TEXT,
                auto_arb_enabled INTEGER NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                volume_usd REAL NOT NULL,
                liquidity_usd REAL NOT NULL,
                market_json TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                PRIMARY KEY (exchange, market_id)
            );

            CREATE TABLE IF NOT EXISTS canonical_events (
                canonical_event_id TEXT PRIMARY KEY,
                link_key TEXT,
                title TEXT NOT NULL,
                event_class TEXT NOT NULL,
                match_lane TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                competition_key TEXT NOT NULL,
                season_key TEXT,
                is_esports INTEGER NOT NULL,
                participants_sorted_json TEXT NOT NULL,
                start_time TEXT,
                best_of INTEGER,
                market_count INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                event_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_links (
                exchange TEXT NOT NULL,
                market_id TEXT NOT NULL,
                canonical_event_id TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL,
                match_lane TEXT NOT NULL,
                manual_override INTEGER NOT NULL DEFAULT 0,
                resolve_compatibility TEXT NOT NULL DEFAULT 'event_only',
                score_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                linked_at TEXT NOT NULL,
                PRIMARY KEY (exchange, market_id)
            );

            CREATE TABLE IF NOT EXISTS latest_quotes (
                exchange TEXT NOT NULL,
                market_id TEXT NOT NULL,
                outcome_id TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                quoted_cost_for_size REAL,
                fee_bps INTEGER NOT NULL,
                slippage_bps_est INTEGER NOT NULL,
                max_fill_usd REAL NOT NULL,
                quote_timestamp TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                quote_json TEXT NOT NULL,
                PRIMARY KEY (exchange, market_id, outcome_id)
            );

            CREATE TABLE IF NOT EXISTS arb_opportunities (
                opportunity_key TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL,
                strategy TEXT NOT NULL,
                status TEXT NOT NULL,
                profit_pct REAL NOT NULL,
                cost REAL NOT NULL,
                gross_payout REAL NOT NULL,
                match_confidence REAL NOT NULL,
                quote_age_seconds REAL NOT NULL,
                reason TEXT,
                detected_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                alerted_at TEXT,
                lifecycle_status TEXT NOT NULL DEFAULT 'live',
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS review_queue (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_key TEXT,
                exchange_a TEXT NOT NULL,
                market_id_a TEXT NOT NULL,
                exchange_b TEXT NOT NULL,
                market_id_b TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL,
                match_lane TEXT NOT NULL,
                veto_reasons_json TEXT NOT NULL,
                score_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                resolved_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS aliases (
                alias_type TEXT NOT NULL,
                sport_key TEXT NOT NULL,
                source_value TEXT NOT NULL,
                canonical_value TEXT NOT NULL,
                PRIMARY KEY (alias_type, sport_key, source_value)
            );
            """
        )
        self._ensure_column("canonical_events", "link_key", "TEXT")
        self._ensure_column("canonical_events", "first_seen_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("canonical_events", "last_seen_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("event_links", "resolve_compatibility", "TEXT NOT NULL DEFAULT 'event_only'")
        self._ensure_column("event_links", "first_seen_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("event_links", "last_seen_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("review_queue", "pair_key", "TEXT")
        self._ensure_column("review_queue", "first_seen_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("review_queue", "last_seen_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("review_queue", "resolved_at", "TEXT")
        self._ensure_column("arb_opportunities", "lifecycle_status", "TEXT NOT NULL DEFAULT 'live'")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_review_queue_pair_key ON review_queue(pair_key)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_event_links_status ON event_links(status, last_seen_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_canonical_events_last_seen ON canonical_events(last_seen_at)")
        self.connection.commit()

    def _ensure_column(self, table_name: str, column_name: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        self.connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def upsert_discovery(
        self,
        exchange: str,
        snapshot_path: Path,
        snapshots: list,
        markets: list[SportsMarket],
    ) -> None:
        raw_rows = [
            (
                snapshot.exchange,
                snapshot.market_id,
                snapshot.fetched_at.isoformat(),
                snapshot_path.as_posix(),
                text_hash(json.dumps(json_ready(snapshot.raw), sort_keys=True)),
                json.dumps(json_ready(snapshot.source), sort_keys=True),
            )
            for snapshot in snapshots
        ]
        cursor = self.connection.cursor()
        cursor.executemany(
            """
            INSERT OR REPLACE INTO raw_snapshots (
                exchange, market_id, fetched_at, snapshot_path, raw_hash, source_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            raw_rows,
        )
        seen_market_ids = self._upsert_markets(cursor, markets)
        if seen_market_ids:
            placeholders = ",".join("?" for _ in seen_market_ids)
            cursor.execute(
                f"UPDATE venue_markets SET active=0 WHERE exchange=? AND market_id NOT IN ({placeholders})",
                [exchange, *sorted(seen_market_ids)],
            )
        else:
            cursor.execute("UPDATE venue_markets SET active=0 WHERE exchange=?", (exchange,))
        self.connection.commit()

    def upsert_market_refresh(self, markets: list[SportsMarket]) -> None:
        cursor = self.connection.cursor()
        self._upsert_markets(cursor, markets)
        self.connection.commit()

    def load_aliases(self) -> dict[tuple[str, str], str]:
        cursor = self.connection.execute(
            "SELECT alias_type, sport_key, source_value, canonical_value FROM aliases"
        )
        return {
            (row["alias_type"], f"{row['sport_key']}:{row['source_value']}"): row["canonical_value"]
            for row in cursor.fetchall()
        }

    def load_active_markets(self) -> list[SportsMarket]:
        cursor = self.connection.execute(
            "SELECT market_json FROM venue_markets WHERE active=1"
        )
        return [self._market_from_json(row["market_json"]) for row in cursor.fetchall()]

    def replace_links(
        self,
        canonical_events: list[SportsCanonicalEvent],
        event_links: list[dict[str, object]],
        review_items: list[dict[str, object]],
    ) -> None:
        now = utc_now().isoformat()
        cursor = self.connection.cursor()
        current_event_ids: set[str] = set()
        current_link_refs: set[tuple[str, str]] = set()
        current_review_keys: set[str] = set()
        for event in canonical_events:
            current_event_ids.add(event.canonical_event_id)
            existing = cursor.execute(
                "SELECT first_seen_at FROM canonical_events WHERE canonical_event_id=?",
                (event.canonical_event_id,),
            ).fetchone()
            cursor.execute(
                """
                INSERT INTO canonical_events (
                    canonical_event_id, link_key, title, event_class, match_lane, sport_key, competition_key, season_key,
                    is_esports, participants_sorted_json, start_time, best_of, market_count, first_seen_at, last_seen_at, updated_at, event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_event_id) DO UPDATE SET
                    link_key=excluded.link_key,
                    title=excluded.title,
                    event_class=excluded.event_class,
                    match_lane=excluded.match_lane,
                    sport_key=excluded.sport_key,
                    competition_key=excluded.competition_key,
                    season_key=excluded.season_key,
                    is_esports=excluded.is_esports,
                    participants_sorted_json=excluded.participants_sorted_json,
                    start_time=excluded.start_time,
                    best_of=excluded.best_of,
                    market_count=excluded.market_count,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at,
                    event_json=excluded.event_json
                """,
                (
                    event.canonical_event_id,
                    event.canonical_event_id,
                    event.title,
                    event.event_class,
                    event.match_lane,
                    event.sport_key,
                    event.competition_key,
                    event.season_key,
                    int(event.is_esports),
                    json.dumps(list(event.participants_sorted), sort_keys=True),
                    event.start_time.isoformat() if event.start_time else None,
                    event.best_of,
                    len(event.markets),
                    existing["first_seen_at"] if existing and existing["first_seen_at"] else now,
                    now,
                    now,
                    json.dumps(json_ready(asdict(event)), sort_keys=True),
                ),
            )
        for link in event_links:
            current_link_refs.add((str(link["exchange"]), str(link["market_id"])))
            existing = cursor.execute(
                "SELECT first_seen_at FROM event_links WHERE exchange=? AND market_id=?",
                (link["exchange"], link["market_id"]),
            ).fetchone()
            cursor.execute(
                """
                INSERT INTO event_links (
                    exchange, market_id, canonical_event_id, confidence, status, match_lane, manual_override,
                    resolve_compatibility, score_json, first_seen_at, last_seen_at, linked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, market_id) DO UPDATE SET
                    canonical_event_id=excluded.canonical_event_id,
                    confidence=excluded.confidence,
                    status=excluded.status,
                    match_lane=excluded.match_lane,
                    manual_override=excluded.manual_override,
                    resolve_compatibility=excluded.resolve_compatibility,
                    score_json=excluded.score_json,
                    last_seen_at=excluded.last_seen_at,
                    linked_at=excluded.linked_at
                """,
                (
                    link["exchange"],
                    link["market_id"],
                    link["canonical_event_id"],
                    link["confidence"],
                    link["status"],
                    link["match_lane"],
                    int(link.get("manual_override", 0)),
                    str(link.get("resolve_compatibility") or "event_only"),
                    json.dumps(json_ready(link.get("score_json", {})), sort_keys=True),
                    existing["first_seen_at"] if existing and existing["first_seen_at"] else now,
                    now,
                    now,
                ),
            )
        for item in review_items:
            pair_key = text_hash(
                "|".join(
                    sorted(
                        [
                            f"{item['exchange_a']}:{item['market_id_a']}",
                            f"{item['exchange_b']}:{item['market_id_b']}",
                        ]
                    )
                )
            )
            current_review_keys.add(pair_key)
            existing = cursor.execute(
                "SELECT review_id, first_seen_at FROM review_queue WHERE pair_key=?",
                (pair_key,),
            ).fetchone()
            cursor.execute(
                """
                INSERT INTO review_queue (
                    pair_key, exchange_a, market_id_a, exchange_b, market_id_b, confidence, status, match_lane,
                    veto_reasons_json, score_json, payload_json, first_seen_at, last_seen_at, resolved_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair_key) DO UPDATE SET
                    confidence=excluded.confidence,
                    status=excluded.status,
                    match_lane=excluded.match_lane,
                    veto_reasons_json=excluded.veto_reasons_json,
                    score_json=excluded.score_json,
                    payload_json=excluded.payload_json,
                    last_seen_at=excluded.last_seen_at,
                    resolved_at=NULL
                """,
                (
                    pair_key,
                    item["exchange_a"],
                    item["market_id_a"],
                    item["exchange_b"],
                    item["market_id_b"],
                    item["confidence"],
                    item["status"],
                    item["match_lane"],
                    json.dumps(item.get("veto_reasons", []), sort_keys=True),
                    json.dumps(json_ready(item.get("score_json", {})), sort_keys=True),
                    json.dumps(json_ready(item.get("payload_json", {})), sort_keys=True),
                    existing["first_seen_at"] if existing and existing["first_seen_at"] else now,
                    now,
                    None,
                    now,
                ),
            )
        if current_review_keys:
            placeholders = ",".join("?" for _ in current_review_keys)
            cursor.execute(
                f"UPDATE review_queue SET resolved_at=COALESCE(resolved_at, ?), status='resolved' WHERE pair_key NOT IN ({placeholders}) AND resolved_at IS NULL",
                [now, *sorted(current_review_keys)],
            )
        else:
            cursor.execute("UPDATE review_queue SET resolved_at=COALESCE(resolved_at, ?), status='resolved' WHERE resolved_at IS NULL", (now,))
        self.connection.commit()

    def load_canonical_events(self, linked_only: bool = False) -> list[SportsCanonicalEvent]:
        events_cursor = self.connection.execute("SELECT canonical_event_id, event_json FROM canonical_events")
        events = {
            row["canonical_event_id"]: self._canonical_event_from_json(row["event_json"])
            for row in events_cursor.fetchall()
        }
        links_query = (
            "SELECT canonical_event_id, exchange, market_id FROM event_links WHERE status='linked'"
            if linked_only
            else "SELECT canonical_event_id, exchange, market_id FROM event_links"
        )
        grouped_market_ids: dict[str, list[tuple[str, str]]] = {}
        for row in self.connection.execute(links_query):
            grouped_market_ids.setdefault(row["canonical_event_id"], []).append((row["exchange"], row["market_id"]))
        markets_by_key = {
            (row["exchange"], row["market_id"]): self._market_from_json(row["market_json"])
            for row in self.connection.execute("SELECT exchange, market_id, market_json FROM venue_markets WHERE active=1")
        }
        loaded: list[SportsCanonicalEvent] = []
        for event_id, event in events.items():
            market_refs = grouped_market_ids.get(event_id, [])
            loaded_markets = [markets_by_key[key] for key in market_refs if key in markets_by_key]
            if linked_only and len({market.exchange for market in loaded_markets}) < 2:
                continue
            loaded.append(
                SportsCanonicalEvent(
                    canonical_event_id=event.canonical_event_id,
                    title=event.title,
                    event_class=event.event_class,
                    match_lane=event.match_lane,
                    sport_key=event.sport_key,
                    competition_key=event.competition_key,
                    season_key=event.season_key,
                    is_esports=event.is_esports,
                    participants_sorted=event.participants_sorted,
                    start_time=event.start_time,
                    best_of=event.best_of,
                    markets=loaded_markets,
                )
            )
        return loaded

    def load_quote_refresh_targets(self) -> list[SportsMarket]:
        events = self.load_canonical_events(linked_only=True)
        cutoff = utc_now() + timedelta(hours=self.settings.sports_quote_priority_hours)
        targets: list[SportsMarket] = []
        for event in events:
            if event.start_time and event.start_time > cutoff:
                continue
            for market in event.markets:
                if market.auto_arb_enabled:
                    targets.append(market)
        return targets

    def upsert_opportunities(self, opportunities: list[SportsOpportunity]) -> list[SportsOpportunity]:
        now = utc_now()
        alerts: list[SportsOpportunity] = []
        cursor = self.connection.cursor()
        current_keys: list[str] = []
        for opportunity in opportunities:
            current_keys.append(opportunity.opportunity_key)
            existing = cursor.execute(
                "SELECT first_seen_at, alerted_at FROM arb_opportunities WHERE opportunity_key=?",
                (opportunity.opportunity_key,),
            ).fetchone()
            alerted_at = parse_datetime(existing["alerted_at"]) if existing and existing["alerted_at"] else None
            should_alert = (
                opportunity.status == "eligible"
                and (alerted_at is None or (now - alerted_at).total_seconds() >= self.settings.telegram_cooldown_seconds)
            )
            first_seen_at = existing["first_seen_at"] if existing else now.isoformat()
            next_alerted_at = opportunity.detected_at.isoformat() if should_alert else (existing["alerted_at"] if existing else None)
            cursor.execute(
                """
                INSERT INTO arb_opportunities (
                    opportunity_key, canonical_event_id, strategy, status, profit_pct, cost, gross_payout,
                    match_confidence, quote_age_seconds, reason, detected_at, first_seen_at, last_seen_at, alerted_at, lifecycle_status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_key) DO UPDATE SET
                    status=excluded.status,
                    profit_pct=excluded.profit_pct,
                    cost=excluded.cost,
                    gross_payout=excluded.gross_payout,
                    match_confidence=excluded.match_confidence,
                    quote_age_seconds=excluded.quote_age_seconds,
                    reason=excluded.reason,
                    detected_at=excluded.detected_at,
                    last_seen_at=excluded.last_seen_at,
                    alerted_at=excluded.alerted_at,
                    lifecycle_status=excluded.lifecycle_status,
                    payload_json=excluded.payload_json
                """,
                (
                    opportunity.opportunity_key,
                    opportunity.canonical_event_id,
                    opportunity.strategy,
                    opportunity.status,
                    opportunity.profit_pct,
                    opportunity.cost,
                    opportunity.gross_payout,
                    opportunity.match_confidence,
                    opportunity.quote_age_seconds,
                    opportunity.reason,
                    opportunity.detected_at.isoformat(),
                    first_seen_at,
                    now.isoformat(),
                    next_alerted_at,
                    opportunity.lifecycle_status,
                    json.dumps(json_ready(asdict(opportunity)), sort_keys=True),
                ),
            )
            if should_alert:
                alerts.append(opportunity)
        if current_keys:
            placeholders = ",".join("?" for _ in current_keys)
            cursor.execute(
                f"UPDATE arb_opportunities SET lifecycle_status='historical' WHERE opportunity_key NOT IN ({placeholders})",
                current_keys,
            )
        else:
            cursor.execute("UPDATE arb_opportunities SET lifecycle_status='historical'")
        self.connection.commit()
        return alerts

    def cleanup(self) -> None:
        now = utc_now()
        raw_cutoff = (now - timedelta(days=7)).isoformat()
        opp_cutoff = (now - timedelta(days=14)).isoformat()
        review_cutoff = (now - timedelta(days=14)).isoformat()
        cursor = self.connection.cursor()
        cursor.execute("DELETE FROM raw_snapshots WHERE fetched_at < ?", (raw_cutoff,))
        cursor.execute("DELETE FROM arb_opportunities WHERE last_seen_at < ?", (opp_cutoff,))
        cursor.execute("DELETE FROM review_queue WHERE resolved_at IS NOT NULL AND last_seen_at < ?", (review_cutoff,))
        cursor.execute(
            """
            DELETE FROM event_links
            WHERE (exchange, market_id) NOT IN (
                SELECT exchange, market_id FROM venue_markets WHERE active=1
            )
            AND last_seen_at < ?
            """,
            (review_cutoff,),
        )
        cursor.execute(
            """
            DELETE FROM canonical_events
            WHERE canonical_event_id NOT IN (
                SELECT DISTINCT canonical_event_id FROM event_links
            )
            AND last_seen_at < ?
            """,
            (review_cutoff,),
        )
        self.connection.commit()

    def counts(self) -> dict[str, int]:
        cursor = self.connection.cursor()
        return {
            "venue_markets": cursor.execute("SELECT COUNT(*) FROM venue_markets WHERE active=1").fetchone()[0],
            "canonical_events": cursor.execute(
                """
                SELECT COUNT(DISTINCT canonical_event_id)
                FROM event_links
                WHERE (exchange, market_id) IN (
                    SELECT exchange, market_id FROM venue_markets WHERE active=1
                )
                """
            ).fetchone()[0],
            "linked_events": cursor.execute(
                """
                SELECT COUNT(DISTINCT canonical_event_id)
                FROM event_links
                WHERE status='linked'
                AND (exchange, market_id) IN (
                    SELECT exchange, market_id FROM venue_markets WHERE active=1
                )
                """
            ).fetchone()[0],
            "review_items": cursor.execute("SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL").fetchone()[0],
        }

    def live_counts(self) -> dict[str, int]:
        counts = self.counts()
        counts.update(
            {
                "opportunities_now": int(
                    self.connection.execute("SELECT COUNT(*) FROM arb_opportunities WHERE lifecycle_status='live'").fetchone()[0]
                )
                if "lifecycle_status" in {
                    str(row["name"]) for row in self.connection.execute("PRAGMA table_info(arb_opportunities)").fetchall()
                }
                else int(self.connection.execute("SELECT COUNT(*) FROM arb_opportunities").fetchone()[0]),
                "eligible_now": int(
                    self.connection.execute("SELECT COUNT(*) FROM arb_opportunities WHERE status='eligible'").fetchone()[0]
                ),
            }
        )
        return counts

    def venue_health(self) -> dict[str, dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT exchange, fetched_at, source_json
            FROM raw_snapshots
            WHERE (exchange, fetched_at) IN (
                SELECT exchange, MAX(fetched_at) FROM raw_snapshots GROUP BY exchange
            )
            """
        ).fetchall()
        health: dict[str, dict[str, object]] = {}
        for row in rows:
            source = json.loads(row["source_json"])
            health[str(row["exchange"])] = {
                "status": str(source.get("status") or "ok"),
                "variant": str(source.get("variant") or "default"),
                "completeness": float(source.get("completeness") or 1.0),
                "last_success_at": str(row["fetched_at"]),
                "notes": list(source.get("notes", [])),
            }
        return health

    @staticmethod
    def _market_from_json(payload_json: str) -> SportsMarket:
        payload = json.loads(payload_json)
        outcomes: list[SportsOutcome] = []
        for outcome_payload in payload.get("outcomes", []):
            quote_payload = outcome_payload["quote"]
            quote = OutcomeQuote(
                outcome_id=str(quote_payload["outcome_id"]),
                label=str(quote_payload["label"]),
                quote_type=str(quote_payload["quote_type"]),
                best_bid=quote_payload.get("best_bid"),
                best_ask=quote_payload.get("best_ask"),
                quoted_cost_for_size=quote_payload.get("quoted_cost_for_size"),
                fee_bps=int(quote_payload["fee_bps"]),
                slippage_bps_est=int(quote_payload["slippage_bps_est"]),
                max_fill_usd=float(quote_payload["max_fill_usd"]),
                quote_timestamp=parse_datetime(quote_payload["quote_timestamp"]),
                metadata=dict(quote_payload.get("metadata", {})),
            )
            outcomes.append(
                SportsOutcome(
                    outcome_id=str(outcome_payload["outcome_id"]),
                    label=str(outcome_payload["label"]),
                    canonical_label=str(outcome_payload["canonical_label"]),
                    selection_key=str(outcome_payload["selection_key"]),
                    quote=quote,
                )
            )
        return SportsMarket(
            exchange=str(payload["exchange"]),
            market_id=str(payload["market_id"]),
            title=str(payload["title"]),
            description=str(payload["description"]),
            event_class=str(payload["event_class"]),
            match_lane=str(payload["match_lane"]),
            sport_key=str(payload["sport_key"]),
            competition_key=str(payload["competition_key"]),
            season_key=payload.get("season_key"),
            is_esports=bool(payload["is_esports"]),
            participants_home=payload.get("participants_home"),
            participants_away=payload.get("participants_away"),
            participants_sorted=tuple(payload.get("participants_sorted", [])),
            start_time=parse_datetime(payload.get("start_time")),
            best_of=payload.get("best_of"),
            period_scope=str(payload["period_scope"]),
            map_no=payload.get("map_no"),
            round_no=payload.get("round_no"),
            market_family=str(payload["market_family"]),
            line_value=payload.get("line_value"),
            line_side=payload.get("line_side"),
            auto_arb_enabled=bool(payload["auto_arb_enabled"]),
            chain=str(payload["chain"]),
            token=str(payload["token"]),
            outcomes=outcomes,
            volume_usd=float(payload["volume_usd"]),
            liquidity_usd=float(payload["liquidity_usd"]),
            fetched_at=parse_datetime(payload["fetched_at"]),
            raw_data=dict(payload.get("raw_data", {})),
            resolution_profile=dict(payload.get("resolution_profile", {})),
            features=dict(payload.get("features", {})),
        )

    @staticmethod
    def _canonical_event_from_json(payload_json: str) -> SportsCanonicalEvent:
        payload = json.loads(payload_json)
        return SportsCanonicalEvent(
            canonical_event_id=str(payload["canonical_event_id"]),
            title=str(payload["title"]),
            event_class=str(payload["event_class"]),
            match_lane=str(payload["match_lane"]),
            sport_key=str(payload["sport_key"]),
            competition_key=str(payload["competition_key"]),
            season_key=payload.get("season_key"),
            is_esports=bool(payload["is_esports"]),
            participants_sorted=tuple(payload.get("participants_sorted", [])),
            start_time=parse_datetime(payload.get("start_time")),
            best_of=payload.get("best_of"),
            markets=[],
        )

    @staticmethod
    def _upsert_markets(cursor: sqlite3.Cursor, markets: list[SportsMarket]) -> set[str]:
        now = utc_now().isoformat()
        seen_market_ids: set[str] = set()
        for market in markets:
            seen_market_ids.add(market.market_id)
            market_payload = json.dumps(json_ready(asdict(market)), sort_keys=True)
            raw_hash = text_hash(market_payload)
            cursor.execute(
                """
                INSERT INTO venue_markets (
                    exchange, market_id, active, updated_at, last_seen_at, title, event_class, match_lane, sport_key,
                    competition_key, season_key, is_esports, participants_home, participants_away, participants_sorted_json,
                    start_time, best_of, period_scope, map_no, round_no, market_family, line_value, line_side,
                    auto_arb_enabled, chain, token, volume_usd, liquidity_usd, market_json, raw_hash
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, market_id) DO UPDATE SET
                    active=1,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at,
                    title=excluded.title,
                    event_class=excluded.event_class,
                    match_lane=excluded.match_lane,
                    sport_key=excluded.sport_key,
                    competition_key=excluded.competition_key,
                    season_key=excluded.season_key,
                    is_esports=excluded.is_esports,
                    participants_home=excluded.participants_home,
                    participants_away=excluded.participants_away,
                    participants_sorted_json=excluded.participants_sorted_json,
                    start_time=excluded.start_time,
                    best_of=excluded.best_of,
                    period_scope=excluded.period_scope,
                    map_no=excluded.map_no,
                    round_no=excluded.round_no,
                    market_family=excluded.market_family,
                    line_value=excluded.line_value,
                    line_side=excluded.line_side,
                    auto_arb_enabled=excluded.auto_arb_enabled,
                    chain=excluded.chain,
                    token=excluded.token,
                    volume_usd=excluded.volume_usd,
                    liquidity_usd=excluded.liquidity_usd,
                    market_json=excluded.market_json,
                    raw_hash=excluded.raw_hash
                """,
                (
                    market.exchange,
                    market.market_id,
                    now,
                    market.fetched_at.isoformat(),
                    market.title,
                    market.event_class,
                    market.match_lane,
                    market.sport_key,
                    market.competition_key,
                    market.season_key,
                    int(market.is_esports),
                    market.participants_home,
                    market.participants_away,
                    json.dumps(list(market.participants_sorted), sort_keys=True),
                    market.start_time.isoformat() if market.start_time else None,
                    market.best_of,
                    market.period_scope,
                    market.map_no,
                    market.round_no,
                    market.market_family,
                    market.line_value,
                    market.line_side,
                    int(market.auto_arb_enabled),
                    market.chain,
                    market.token,
                    market.volume_usd,
                    market.liquidity_usd,
                    market_payload,
                    raw_hash,
                ),
            )
            for outcome in market.outcomes:
                cursor.execute(
                    """
                    INSERT INTO latest_quotes (
                        exchange, market_id, outcome_id, fetched_at, best_bid, best_ask, quoted_cost_for_size, fee_bps,
                        slippage_bps_est, max_fill_usd, quote_timestamp, selection_key, quote_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(exchange, market_id, outcome_id) DO UPDATE SET
                        fetched_at=excluded.fetched_at,
                        best_bid=excluded.best_bid,
                        best_ask=excluded.best_ask,
                        quoted_cost_for_size=excluded.quoted_cost_for_size,
                        fee_bps=excluded.fee_bps,
                        slippage_bps_est=excluded.slippage_bps_est,
                        max_fill_usd=excluded.max_fill_usd,
                        quote_timestamp=excluded.quote_timestamp,
                        selection_key=excluded.selection_key,
                        quote_json=excluded.quote_json
                    """,
                    (
                        market.exchange,
                        market.market_id,
                        outcome.outcome_id,
                        now,
                        outcome.quote.best_bid,
                        outcome.quote.best_ask,
                        outcome.quote.quoted_cost_for_size,
                        outcome.quote.fee_bps,
                        outcome.quote.slippage_bps_est,
                        outcome.quote.max_fill_usd,
                        outcome.quote.quote_timestamp.isoformat(),
                        outcome.selection_key,
                        json.dumps(json_ready(asdict(outcome.quote)), sort_keys=True),
                    ),
                )
        return seen_market_ids
