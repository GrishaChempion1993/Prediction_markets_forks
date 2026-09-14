from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import json
import sqlite3

from .config import Settings
from .models import ArbitrageOpportunity, MatchedPair, NormalizedMarket, NormalizedOutcome, OutcomeQuote
from .utils import ensure_parent, json_ready, parse_datetime, text_hash, utc_now


class MarketCache:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.repo_root / settings.market_db_path
        ensure_parent(self.path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
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
                matched_locked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                title TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                category_family TEXT NOT NULL,
                closes_at TEXT,
                resolution_rules_hash TEXT NOT NULL,
                market_json TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                PRIMARY KEY (exchange, market_id)
            );

            CREATE TABLE IF NOT EXISTS matched_pairs (
                pair_key TEXT PRIMARY KEY,
                exchange_a TEXT NOT NULL,
                market_id_a TEXT NOT NULL,
                exchange_b TEXT NOT NULL,
                market_id_b TEXT NOT NULL,
                confidence REAL NOT NULL,
                match_method TEXT NOT NULL,
                outcome_mapping_json TEXT NOT NULL,
                score_json TEXT NOT NULL,
                resolve_compatibility TEXT NOT NULL DEFAULT 'compatible',
                veto_reasons_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'live',
                inactive_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS arb_opportunities (
                opportunity_key TEXT PRIMARY KEY,
                pair_key TEXT NOT NULL,
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
            """
        )
        self._ensure_column("matched_pairs", "resolve_compatibility", "TEXT NOT NULL DEFAULT 'compatible'")
        self._ensure_column("matched_pairs", "state", "TEXT NOT NULL DEFAULT 'live'")
        self._ensure_column("matched_pairs", "inactive_reason", "TEXT")
        self._ensure_column("arb_opportunities", "lifecycle_status", "TEXT NOT NULL DEFAULT 'live'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_venue_markets_active_locked ON venue_markets(active, matched_locked)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matched_pairs_state ON matched_pairs(state)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_arb_lifecycle_status ON arb_opportunities(lifecycle_status, status)")
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
        markets: list[NormalizedMarket],
    ) -> list[NormalizedMarket]:
        cursor = self.connection.cursor()
        existing_rows = {
            str(row["market_id"]): row
            for row in cursor.execute(
                "SELECT market_id, active, raw_hash FROM venue_markets WHERE exchange=?",
                (exchange,),
            ).fetchall()
        }
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
        cursor.executemany(
            """
            INSERT OR REPLACE INTO raw_snapshots (
                exchange, market_id, fetched_at, snapshot_path, raw_hash, source_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            raw_rows,
        )

        seen_market_ids: set[str] = set()
        frontier_markets: list[NormalizedMarket] = []
        now = utc_now().isoformat()
        for market in markets:
            seen_market_ids.add(market.market_id)
            raw_hash = text_hash(json.dumps(json_ready(market.raw_data), sort_keys=True))
            existing = existing_rows.get(market.market_id)
            if (
                existing is None
                or str(existing["raw_hash"]) != raw_hash
                or int(existing["active"]) == 0
            ):
                frontier_markets.append(market)
            cursor.execute(
                """
                INSERT INTO venue_markets (
                    exchange, market_id, active, matched_locked, updated_at, last_seen_at, title, event_kind,
                    category_family, closes_at, resolution_rules_hash, market_json, raw_hash
                ) VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, market_id) DO UPDATE SET
                    active=1,
                    updated_at=excluded.updated_at,
                    last_seen_at=excluded.last_seen_at,
                    title=excluded.title,
                    event_kind=excluded.event_kind,
                    category_family=excluded.category_family,
                    closes_at=excluded.closes_at,
                    resolution_rules_hash=excluded.resolution_rules_hash,
                    market_json=excluded.market_json,
                    raw_hash=excluded.raw_hash
                """,
                (
                    market.exchange,
                    market.market_id,
                    now,
                    now,
                    market.title,
                    market.event_kind,
                    market.category_family,
                    market.closes_at.isoformat() if market.closes_at else None,
                    market.resolution_rules_hash,
                    json.dumps(json_ready(asdict(market)), sort_keys=True),
                    raw_hash,
                ),
            )

        if seen_market_ids:
            placeholders = ",".join("?" for _ in seen_market_ids)
            cursor.execute(
                f"UPDATE venue_markets SET active=0 WHERE exchange=? AND market_id NOT IN ({placeholders})",
                [exchange, *sorted(seen_market_ids)],
            )
        else:
            cursor.execute("UPDATE venue_markets SET active=0 WHERE exchange=?", (exchange,))
        cursor.execute("UPDATE venue_markets SET matched_locked=0 WHERE active=0")
        self.connection.commit()
        return frontier_markets

    def load_unmatched_markets(self) -> list[NormalizedMarket]:
        cursor = self.connection.execute(
            "SELECT market_json FROM venue_markets WHERE active=1 AND matched_locked=0"
        )
        return [self._market_from_json(row["market_json"]) for row in cursor.fetchall()]

    def load_active_markets(self) -> list[NormalizedMarket]:
        cursor = self.connection.execute("SELECT market_json FROM venue_markets WHERE active=1")
        return [self._market_from_json(row["market_json"]) for row in cursor.fetchall()]

    def refresh_lifecycle(self) -> None:
        cursor = self.connection.cursor()
        now = utc_now()
        stale_cutoff = now - timedelta(seconds=self.settings.quote_ttl_seconds)
        rows = cursor.execute(
            """
            SELECT
                pair_key,
                exchange_a,
                market_id_a,
                exchange_b,
                market_id_b,
                a.active AS active_a,
                b.active AS active_b,
                a.updated_at AS updated_at_a,
                b.updated_at AS updated_at_b
            FROM matched_pairs
            LEFT JOIN venue_markets AS a ON a.exchange = matched_pairs.exchange_a AND a.market_id = matched_pairs.market_id_a
            LEFT JOIN venue_markets AS b ON b.exchange = matched_pairs.exchange_b AND b.market_id = matched_pairs.market_id_b
            """
        ).fetchall()
        for row in rows:
            inactive_reason = None
            updated_at_a = parse_datetime(row["updated_at_a"])
            updated_at_b = parse_datetime(row["updated_at_b"])
            if int(row["active_a"] or 0) != 1 or int(row["active_b"] or 0) != 1:
                inactive_reason = "market_inactive"
            elif (updated_at_a and updated_at_a < stale_cutoff) or (updated_at_b and updated_at_b < stale_cutoff):
                inactive_reason = "quote_stale"
            if inactive_reason:
                cursor.execute(
                    """
                    UPDATE matched_pairs
                    SET state='inactive', inactive_reason=?, last_seen_at=?
                    WHERE pair_key=?
                    """,
                    (inactive_reason, now.isoformat(), row["pair_key"]),
                )
                cursor.execute(
                    """
                    UPDATE venue_markets
                    SET matched_locked=0
                    WHERE (exchange=? AND market_id=?) OR (exchange=? AND market_id=?)
                    """,
                    (row["exchange_a"], row["market_id_a"], row["exchange_b"], row["market_id_b"]),
                )
            else:
                cursor.execute(
                    "UPDATE matched_pairs SET state='live', inactive_reason=NULL WHERE pair_key=?",
                    (row["pair_key"],),
                )
        self.connection.commit()

    def store_pairs(self, pairs: list[MatchedPair]) -> list[MatchedPair]:
        cursor = self.connection.cursor()
        stored: list[MatchedPair] = []
        now = utc_now().isoformat()
        for pair in pairs:
            pair_key = self._pair_key(pair.market_a.exchange, pair.market_a.market_id, pair.market_b.exchange, pair.market_b.market_id)
            exists = cursor.execute("SELECT 1 FROM matched_pairs WHERE pair_key=?", (pair_key,)).fetchone()
            cursor.execute(
                """
                INSERT INTO matched_pairs (
                    pair_key, exchange_a, market_id_a, exchange_b, market_id_b, confidence, match_method,
                    outcome_mapping_json, score_json, resolve_compatibility, veto_reasons_json, first_seen_at, last_seen_at, state, inactive_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair_key) DO UPDATE SET
                    confidence=excluded.confidence,
                    match_method=excluded.match_method,
                    outcome_mapping_json=excluded.outcome_mapping_json,
                    score_json=excluded.score_json,
                    resolve_compatibility=excluded.resolve_compatibility,
                    veto_reasons_json=excluded.veto_reasons_json,
                    last_seen_at=excluded.last_seen_at,
                    state='live',
                    inactive_reason=NULL
                """,
                (
                    pair_key,
                    pair.market_a.exchange,
                    pair.market_a.market_id,
                    pair.market_b.exchange,
                    pair.market_b.market_id,
                    pair.confidence,
                    pair.match_method,
                    json.dumps(pair.outcome_mapping, sort_keys=True),
                    json.dumps(json_ready(pair.score_breakdown), sort_keys=True),
                    pair.resolve_compatibility,
                    json.dumps(pair.veto_reasons, sort_keys=True),
                    now,
                    now,
                    "live",
                    None,
                ),
            )
            cursor.execute(
                "UPDATE venue_markets SET matched_locked=1 WHERE (exchange=? AND market_id=?) OR (exchange=? AND market_id=?)",
                (pair.market_a.exchange, pair.market_a.market_id, pair.market_b.exchange, pair.market_b.market_id),
            )
            if not exists:
                stored.append(pair)
        self.connection.commit()
        return stored

    def load_pairs(self) -> list[MatchedPair]:
        market_rows = {
            (row["exchange"], row["market_id"]): self._market_from_json(row["market_json"])
            for row in self.connection.execute("SELECT exchange, market_id, market_json FROM venue_markets WHERE active=1")
        }
        pairs: list[MatchedPair] = []
        for row in self.connection.execute("SELECT * FROM matched_pairs WHERE state='live' ORDER BY last_seen_at DESC"):
            market_a = market_rows.get((row["exchange_a"], row["market_id_a"]))
            market_b = market_rows.get((row["exchange_b"], row["market_id_b"]))
            if market_a is None or market_b is None:
                continue
            pairs.append(
                MatchedPair(
                    market_a=market_a,
                    market_b=market_b,
                    confidence=float(row["confidence"]),
                    match_method=str(row["match_method"]),
                    outcome_mapping=dict(json.loads(row["outcome_mapping_json"])),
                    score_breakdown=dict(json.loads(row["score_json"])),
                    resolve_compatibility=str(row["resolve_compatibility"] or "compatible"),
                    veto_reasons=list(json.loads(row["veto_reasons_json"])),
                )
            )
        return pairs

    def upsert_opportunities(self, opportunities: list[ArbitrageOpportunity]) -> list[ArbitrageOpportunity]:
        cursor = self.connection.cursor()
        alerts: list[ArbitrageOpportunity] = []
        now = utc_now()
        current_keys: list[str] = []
        for opportunity in opportunities:
            pair_key = self._pair_key(
                opportunity.market_a.exchange,
                opportunity.market_a.market_id,
                opportunity.market_b.exchange,
                opportunity.market_b.market_id,
            )
            opportunity_key = text_hash(f"{pair_key}|{opportunity.strategy}")
            existing = cursor.execute(
                "SELECT first_seen_at, alerted_at FROM arb_opportunities WHERE opportunity_key=?",
                (opportunity_key,),
            ).fetchone()
            alerted_at = parse_datetime(existing["alerted_at"]) if existing and existing["alerted_at"] else None
            should_alert = (
                opportunity.status == "eligible"
                and (alerted_at is None or (now - alerted_at).total_seconds() >= self.settings.telegram_cooldown_seconds)
            )
            current_keys.append(opportunity_key)
            cursor.execute(
                """
                INSERT INTO arb_opportunities (
                    opportunity_key, pair_key, status, profit_pct, cost, gross_payout, match_confidence,
                    quote_age_seconds, reason, detected_at, first_seen_at, last_seen_at, alerted_at, lifecycle_status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    opportunity_key,
                    pair_key,
                    opportunity.status,
                    opportunity.profit_pct,
                    opportunity.cost,
                    opportunity.gross_payout,
                    opportunity.match_confidence,
                    opportunity.quote_age_seconds,
                    opportunity.reason,
                    now.isoformat(),
                    existing["first_seen_at"] if existing else now.isoformat(),
                    now.isoformat(),
                    now.isoformat() if should_alert else (existing["alerted_at"] if existing else None),
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

    def counts(self) -> dict[str, int]:
        cursor = self.connection.cursor()
        return {
            "active_markets": int(cursor.execute("SELECT COUNT(*) FROM venue_markets WHERE active=1").fetchone()[0]),
            "locked_markets": int(cursor.execute("SELECT COUNT(*) FROM venue_markets WHERE matched_locked=1").fetchone()[0]),
            "matched_pairs": int(cursor.execute("SELECT COUNT(*) FROM matched_pairs").fetchone()[0]),
            "opportunities": int(cursor.execute("SELECT COUNT(*) FROM arb_opportunities").fetchone()[0]),
        }

    def live_counts(self) -> dict[str, int]:
        self.refresh_lifecycle()
        cursor = self.connection.cursor()
        return {
            "active_markets_now": int(cursor.execute("SELECT COUNT(*) FROM venue_markets WHERE active=1").fetchone()[0]),
            "locked_markets_now": int(cursor.execute("SELECT COUNT(*) FROM venue_markets WHERE active=1 AND matched_locked=1").fetchone()[0]),
            "fresh_pairs_now": int(cursor.execute("SELECT COUNT(*) FROM matched_pairs WHERE state='live'").fetchone()[0]),
            "stale_pairs_now": int(cursor.execute("SELECT COUNT(*) FROM matched_pairs WHERE state!='live'").fetchone()[0]),
            "opportunities_now": int(cursor.execute("SELECT COUNT(*) FROM arb_opportunities WHERE lifecycle_status='live'").fetchone()[0]),
            "eligible_now": int(
                cursor.execute("SELECT COUNT(*) FROM arb_opportunities WHERE lifecycle_status='live' AND status='eligible'").fetchone()[0]
            ),
        }

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

    def cleanup(self) -> None:
        self.refresh_lifecycle()
        now = utc_now()
        raw_cutoff = (now - timedelta(hours=48)).isoformat()
        inactive_market_cutoff = (now - timedelta(hours=24)).isoformat()
        pair_cutoff = (now - timedelta(days=7)).isoformat()
        opportunity_cutoff = (now - timedelta(days=7)).isoformat()
        cursor = self.connection.cursor()
        cursor.execute("DELETE FROM raw_snapshots WHERE fetched_at < ?", (raw_cutoff,))
        cursor.execute("DELETE FROM venue_markets WHERE active=0 AND last_seen_at < ?", (inactive_market_cutoff,))
        cursor.execute("DELETE FROM matched_pairs WHERE state!='live' AND last_seen_at < ?", (pair_cutoff,))
        cursor.execute(
            """
            DELETE FROM arb_opportunities
            WHERE lifecycle_status!='live' AND last_seen_at < ?
            """,
            (opportunity_cutoff,),
        )
        self.connection.commit()

    @staticmethod
    def _pair_key(exchange_a: str, market_id_a: str, exchange_b: str, market_id_b: str) -> str:
        left, right = sorted([f"{exchange_a}:{market_id_a}", f"{exchange_b}:{market_id_b}"])
        return text_hash(f"{left}|{right}")

    def _market_from_json(self, payload: str) -> NormalizedMarket:
        data = json.loads(payload)
        outcomes = [
            NormalizedOutcome(
                outcome_id=str(item["outcome_id"]),
                label=str(item["label"]),
                canonical_label=str(item["canonical_label"]),
                quote=OutcomeQuote(
                    outcome_id=str(item["quote"]["outcome_id"]),
                    label=str(item["quote"]["label"]),
                    quote_type=str(item["quote"]["quote_type"]),
                    best_bid=item["quote"]["best_bid"],
                    best_ask=item["quote"]["best_ask"],
                    quoted_cost_for_size=item["quote"]["quoted_cost_for_size"],
                    fee_bps=int(item["quote"]["fee_bps"]),
                    slippage_bps_est=int(item["quote"]["slippage_bps_est"]),
                    max_fill_usd=float(item["quote"]["max_fill_usd"]),
                    quote_timestamp=parse_datetime(item["quote"]["quote_timestamp"]) or utc_now(),
                    metadata=dict(item["quote"].get("metadata", {})),
                ),
            )
            for item in data["outcomes"]
        ]
        return NormalizedMarket(
            exchange=str(data["exchange"]),
            market_id=str(data["market_id"]),
            market_type=str(data["market_type"]),
            event_kind=str(data["event_kind"]),
            title=str(data["title"]),
            description=str(data["description"]),
            category=str(data["category"]),
            category_family=str(data["category_family"]),
            tags=list(data["tags"]),
            chain=str(data["chain"]),
            token=str(data["token"]),
            closes_at=parse_datetime(data.get("closes_at")),
            resolves_at=parse_datetime(data.get("resolves_at")),
            resolution_source_url=data.get("resolution_source_url"),
            resolution_rules_hash=str(data["resolution_rules_hash"]),
            outcomes=outcomes,
            volume_usd=float(data["volume_usd"]),
            liquidity_usd=float(data["liquidity_usd"]),
            fetched_at=parse_datetime(data["fetched_at"]) or utc_now(),
            raw_data=dict(data["raw_data"]),
            resolution_profile=dict(data.get("resolution_profile", {})),
            features=dict(data.get("features", {})),
        )
