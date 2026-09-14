from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from arb_engine.config import Settings
from arb_engine.market_db import MarketCache
from arb_engine.matcher import MarketMatcher
from arb_engine.models import MarketSnapshot
from arb_engine.normalize import normalize_snapshots
from arb_engine.scanner import OpportunityScanner
from arb_engine.sports_db import SportsCache
from arb_engine.sports_matcher import SportsEventMatcher
from arb_engine.sports_normalize import normalize_sports_snapshots
from arb_engine.sports_scanner import SportsArbitrageScanner
from arb_engine.telegram import TelegramNotifier
from arb_engine.utils import parse_datetime


FIXTURES = Path(__file__).parent / "fixtures"


def load_snapshot(name: str) -> MarketSnapshot:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return MarketSnapshot(
        schema_version=payload["schema_version"],
        exchange=payload["exchange"],
        market_id=payload["market_id"],
        fetched_at=parse_datetime(payload["fetched_at"]),
        source=payload["source"],
        raw=payload["raw"],
    )


def make_snapshot(exchange: str, market_id: str, raw: dict[str, object], fetched_at: str = "2026-03-10T12:00:00Z") -> MarketSnapshot:
    return MarketSnapshot(
        schema_version="market_snapshot.v1",
        exchange=exchange,
        market_id=market_id,
        fetched_at=parse_datetime(fetched_at),
        source={"collector": "test", "endpoints": ["fixture"]},
        raw=raw,
    )


def make_polymarket_match_snapshot(market_id: str, title: str, yes_price: str, no_price: str, end_date: str) -> MarketSnapshot:
    return make_snapshot(
        "polymarket",
        market_id,
        {
            "question": title,
            "description": "Professional Boxing match market",
            "groupItemTitle": "Professional Boxing",
            "endDate": end_date,
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps([yes_price, no_price]),
            "clobTokenIds": json.dumps([f"{market_id}-yes", f"{market_id}-no"]),
            "orderBooks": {
                f"{market_id}-yes": {"asks": [{"price": yes_price, "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
                f"{market_id}-no": {"asks": [{"price": no_price, "size": "200"}], "bids": [{"price": "0.50", "size": "200"}]},
            },
            "volumeNum": "10000",
            "liquidityNum": "5000",
        },
    )


def make_azuro_match_snapshot(
    market_id: str,
    title: str,
    participants: list[str],
    odds: list[str],
    starts_at: str = "1773178800",
    sport_name: str = "Boxing",
) -> MarketSnapshot:
    return make_snapshot(
        "azuro",
        market_id,
        {
            "environment": "PolygonUSDT",
            "game": {
                "gameId": f"game-{market_id}",
                "title": title,
                "startsAt": starts_at,
                "turnover": "8000",
                "sport": {"name": sport_name, "slug": sport_name.lower()},
                "league": {"name": f"{sport_name} League", "slug": f"{sport_name.lower()}-league"},
                "participants": [{"name": participants[0]}, {"name": participants[1]}],
            },
            "condition": {
                "conditionId": market_id,
                "margin": 0,
                "outcomes": [
                    {"outcomeId": f"{market_id}-1", "odds": odds[0]},
                    {"outcomeId": f"{market_id}-2", "odds": odds[1]},
                ],
                "game": {"gameId": f"game-{market_id}"},
            },
        },
    )


def make_azuro_total_snapshot(market_id: str, title: str, participants: list[str], line: str) -> MarketSnapshot:
    return make_snapshot(
        "azuro",
        market_id,
        {
            "environment": "PolygonUSDT",
            "game": {
                "gameId": f"game-{market_id}",
                "title": title,
                "startsAt": "1773342000",
                "turnover": "9000",
                "sport": {"name": "Basketball", "slug": "basketball"},
                "league": {"name": "NBA", "slug": "nba"},
                "participants": [{"name": participants[0]}, {"name": participants[1]}],
            },
            "condition": {
                "conditionId": market_id,
                "title": f"Total {line}",
                "margin": 0,
                "outcomes": [
                    {"title": f"Over {line}", "outcomeId": f"{market_id}-over", "odds": "2.05"},
                    {"title": f"Under {line}", "outcomeId": f"{market_id}-under", "odds": "1.95"},
                ],
                "game": {"gameId": f"game-{market_id}"},
            },
        },
    )


def make_myriad_snapshot(
    market_id: str,
    title: str,
    outcomes: list[tuple[str, str]],
    resolves_at: str,
    category: str = "esports",
    topics: list[str] | None = None,
    description: str = "",
) -> MarketSnapshot:
    return make_snapshot(
        "myriad",
        market_id,
        {
            "id": int(market_id),
            "networkId": 274133,
            "title": title,
            "description": description,
            "category": category,
            "topics": topics or [category],
            "resolvesAt": resolves_at,
            "expiresAt": resolves_at,
            "volume": "12000",
            "liquidity": "6000",
            "token": {"symbol": "USDC"},
            "outcomes": [{"id": f"{market_id}-{index}", "title": label, "price": price} for index, (label, price) in enumerate(outcomes, start=1)],
        },
    )


def make_opn_snapshot(
    market_id: str,
    title: str,
    outcomes: list[tuple[str, str]],
    closes_at: str,
    category: str = "crypto",
) -> MarketSnapshot:
    return make_snapshot(
        "opn",
        market_id,
        {
            "id": market_id,
            "title": title,
            "description": "Opinion test market",
            "category": category,
            "closesAt": closes_at,
            "liquidity": "10000",
            "volume": "5000",
            "outcomes": [{"title": label, "price": price} for label, price in outcomes],
        },
    )


def make_opn_web_snapshot(
    market_id: str,
    title: str,
    yes_label: str,
    no_label: str,
    yes_buy_price: str,
    no_buy_price: str,
    cutoff_time: str,
    labels: list[str] | None = None,
) -> MarketSnapshot:
    return make_snapshot(
        "opn",
        market_id,
        {
            "topicId": market_id,
            "slug": market_id,
            "title": title,
            "abstract": "",
            "labelName": labels or ["Sports"],
            "cutoffTime": cutoff_time,
            "volume": "15",
            "totalPrice": "15",
            "yesLabel": yes_label,
            "noLabel": no_label,
            "yesBuyPrice": yes_buy_price,
            "noBuyPrice": no_buy_price,
            "rules": f'If {yes_label} wins the market resolves to "{yes_label}". Resolution Source: https://www.nba.com/',
        },
    )


def make_opn_web_child_snapshot(
    market_id: str,
    title: str,
    outcomes: list[tuple[str, str]],
    cutoff_time: str,
    category: str = "Politics",
) -> MarketSnapshot:
    return make_snapshot(
        "opn",
        market_id,
        {
            "topicId": market_id,
            "slug": market_id,
            "title": title,
            "labelName": [category],
            "cutoffTime": cutoff_time,
            "volume": "1000",
            "totalPrice": "1500",
            "rules": "Official appointment resolves the market.",
            "childList": [
                {
                    "topicId": f"{market_id}-{index}",
                    "slug": f"{market_id}-{index}",
                    "title": label,
                    "yesBuyPrice": price,
                    "yesMarketPrice": price,
                }
                for index, (label, price) in enumerate(outcomes, start=1)
            ],
        },
    )


def make_kalshi_snapshot(
    market_id: str,
    title: str,
    yes_label: str,
    no_label: str,
    yes_ask: str,
    no_ask: str,
    close_time: str,
    event_ticker: str = "KXNBAGAME-26MAR11CHASAC",
    rules_primary: str | None = None,
) -> MarketSnapshot:
    return make_snapshot(
        "kalshi",
        market_id,
        {
            "ticker": market_id,
            "event_ticker": event_ticker,
            "title": title,
            "rules_primary": rules_primary or f"If {yes_label} wins the professional basketball game, then the market resolves to Yes.",
            "yes_sub_title": yes_label,
            "no_sub_title": no_label,
            "yes_ask_dollars": yes_ask,
            "yes_bid_dollars": "0.86",
            "yes_ask_size_fp": "46536.00",
            "no_ask_dollars": no_ask,
            "no_bid_dollars": "0.13",
            "no_ask_size_fp": "51028.00",
            "volume_fp": "8262.00",
            "liquidity_dollars": "0.0000",
            "close_time": close_time,
            "expiration_time": close_time,
            "market_type": "binary",
            "status": "active",
        },
    )


def make_sxbet_snapshot(
    market_id: str,
    team_one: str,
    team_two: str,
    game_time: str,
    market_type: int = 52,
    orders: list[dict[str, object]] | None = None,
    sport_label: str = "Soccer",
    league_label: str = "Champions League_UEFA",
    group1: str = "Champions League",
    group2: str = "UEFA",
) -> MarketSnapshot:
    return make_snapshot(
        "sxbet",
        market_id,
        {
            "marketHash": market_id,
            "teamOneName": team_one,
            "teamTwoName": team_two,
            "outcomeOneName": team_one,
            "outcomeTwoName": team_two,
            "gameTime": game_time,
            "sportLabel": sport_label,
            "leagueLabel": league_label,
            "group1": group1,
            "group2": group2,
            "type": market_type,
            "orders": orders
            or [
                {
                    "marketHash": market_id,
                    "percentageOdds": "4500000000000000000",
                    "totalBetSize": "150000000",
                    "isMakerBettingOutcomeOne": True,
                },
                {
                    "marketHash": market_id,
                    "percentageOdds": "5200000000000000000",
                    "totalBetSize": "170000000",
                    "isMakerBettingOutcomeOne": False,
                },
            ],
        },
    )


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(repo_root=Path.cwd(), min_profit_pct=1.0, quote_ttl_seconds=400_000_000)

    def test_polymarket_orderbook_quote_uses_depth(self) -> None:
        market = normalize_snapshots([load_snapshot("polymarket_btc_100k.json")], self.settings)[0]
        yes_outcome = next(outcome for outcome in market.outcomes if outcome.label == "Yes")
        self.assertAlmostEqual(yes_outcome.quote.best_ask or 0.0, 0.53, places=4)
        self.assertAlmostEqual(yes_outcome.quote.quoted_cost_for_size or 0.0, 53.0, places=4)
        self.assertGreaterEqual(yes_outcome.quote.max_fill_usd, 200.0)

    def test_matcher_rejects_different_numeric_thresholds(self) -> None:
        markets = normalize_snapshots(
            [load_snapshot("polymarket_btc_100k.json"), load_snapshot("myriad_btc_150k.json")],
            self.settings,
        )
        matcher = MarketMatcher(self.settings)
        matched_pairs, _ = matcher.match(markets)
        self.assertEqual(matched_pairs, [])

    def test_matcher_rejects_championship_vs_single_game(self) -> None:
        markets = normalize_snapshots(
            [load_snapshot("polymarket_lakers_championship.json"), load_snapshot("azuro_lakers_game.json")],
            self.settings,
        )
        matcher = MarketMatcher(self.settings)
        matched_pairs, _ = matcher.match(markets)
        self.assertEqual(matched_pairs, [])

    def test_scanner_finds_binary_opportunity(self) -> None:
        markets = normalize_snapshots(
            [load_snapshot("polymarket_btc_100k.json"), load_snapshot("myriad_btc_100k.json")],
            self.settings,
        )
        matcher = MarketMatcher(self.settings)
        matched_pairs, _ = matcher.match(markets)
        self.assertEqual(len(matched_pairs), 1)
        scanner = OpportunityScanner(self.settings)
        opportunities = scanner.scan(matched_pairs)
        self.assertTrue(opportunities)
        best = opportunities[0]
        self.assertGreater(best.profit_pct, 0.0)
        self.assertIn(best.status, {"eligible", "review_only"})

    def test_scanner_rejects_stale_quotes(self) -> None:
        stale_payload = deepcopy(json.loads((FIXTURES / "myriad_btc_100k.json").read_text(encoding="utf-8")))
        stale_payload["fetched_at"] = "2025-01-01T00:00:00Z"
        stale_snapshot = MarketSnapshot(
            schema_version=stale_payload["schema_version"],
            exchange=stale_payload["exchange"],
            market_id=stale_payload["market_id"],
            fetched_at=parse_datetime(stale_payload["fetched_at"]),
            source=stale_payload["source"],
            raw=stale_payload["raw"],
        )
        markets = normalize_snapshots(
            [load_snapshot("polymarket_btc_100k.json"), stale_snapshot],
            Settings(repo_root=Path.cwd(), min_profit_pct=1.0, quote_ttl_seconds=60),
        )
        matcher = MarketMatcher(self.settings)
        matched_pairs, _ = matcher.match(markets)
        scanner = OpportunityScanner(Settings(repo_root=Path.cwd(), min_profit_pct=1.0, quote_ttl_seconds=60))
        opportunities = scanner.scan(matched_pairs)
        self.assertTrue(opportunities)
        self.assertEqual(opportunities[0].status, "rejected")
        self.assertEqual(opportunities[0].reason, "stale_quote")

    def test_telegram_notifier_deduplicates_signals(self) -> None:
        markets = normalize_snapshots(
            [load_snapshot("polymarket_btc_100k.json"), load_snapshot("myriad_btc_100k.json")],
            self.settings,
        )
        matcher = MarketMatcher(self.settings)
        matched_pairs, _ = matcher.match(markets)
        opportunity = OpportunityScanner(self.settings).scan(matched_pairs)[0]

        sent_messages: list[str] = []
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                repo_root=Path.cwd(),
                logs_root=Path(temp_dir) / "logs",
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_min_profit_pct=1.0,
                telegram_statuses=("eligible",),
                telegram_cooldown_seconds=3600,
            )
            notifier = TelegramNotifier(settings, send_func=sent_messages.append)
            first = notifier.notify_opportunities([opportunity], "monitor")
            second = notifier.notify_opportunities([opportunity], "monitor")

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(sent_messages), 1)
        self.assertIn("Arbitrage signal", sent_messages[0])
        self.assertIn("Profit:", sent_messages[0])

    def test_telegram_logs_command_returns_runtime_tail(self) -> None:
        with TemporaryDirectory() as temp_dir:
            logs_root = Path(temp_dir) / "logs"
            logs_root.mkdir(parents=True, exist_ok=True)
            (logs_root / "runtime.log").write_text("one\ntwo\nthree\n", encoding="utf-8")

            settings = Settings(
                repo_root=Path(temp_dir),
                logs_root=Path("logs"),
                telegram_bot_token="token",
                telegram_chat_id="chat",
            )
            notifier = TelegramNotifier(settings)
            captured: list[dict[str, object]] = []

            def fake_send_message(text: str, reply_markup=None, chat_id=None, parse_mode=None) -> dict[str, object]:
                captured.append(
                    {
                        "text": text,
                        "reply_markup": reply_markup,
                        "chat_id": chat_id,
                        "parse_mode": parse_mode,
                    }
                )
                return {}

            notifier._send_message = fake_send_message  # type: ignore[method-assign]
            notifier._handle_message({"chat": {"id": "chat"}, "text": "/logs 2"})

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["chat_id"], "chat")
        self.assertEqual(captured[0]["parse_mode"], "HTML")
        self.assertIn("two", str(captured[0]["text"]))
        self.assertIn("three", str(captured[0]["text"]))
        self.assertNotIn("one", str(captured[0]["text"]))
        self.assertEqual(captured[0]["reply_markup"], {"inline_keyboard": [[{"text": "Delete", "callback_data": "delete"}]]})

    def test_telegram_status_command_returns_summary(self) -> None:
        with TemporaryDirectory() as temp_dir:
            logs_root = Path(temp_dir) / "logs"
            logs_root.mkdir(parents=True, exist_ok=True)
            (logs_root / "runtime.log").write_text("last runtime line\n", encoding="utf-8")
            (logs_root / "matches.jsonl").write_text("{}\n{}\n", encoding="utf-8")
            (logs_root / "opportunities.jsonl").write_text("{}\n", encoding="utf-8")

            settings = Settings(
                repo_root=Path(temp_dir),
                logs_root=Path("logs"),
                telegram_bot_token="token",
                telegram_chat_id="chat",
            )
            notifier = TelegramNotifier(settings)
            captured: list[dict[str, object]] = []

            def fake_send_message(text: str, reply_markup=None, chat_id=None, parse_mode=None) -> dict[str, object]:
                captured.append(
                    {
                        "text": text,
                        "reply_markup": reply_markup,
                        "chat_id": chat_id,
                        "parse_mode": parse_mode,
                    }
                )
                return {}

            notifier._send_message = fake_send_message  # type: ignore[method-assign]
            notifier._handle_message({"chat": {"id": "chat"}, "text": "/status"})

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["chat_id"], "chat")
        self.assertEqual(captured[0]["parse_mode"], "HTML")
        self.assertIn("<b>Generic</b>", str(captured[0]["text"]))
        self.assertIn("active_markets_now=0", str(captured[0]["text"]))
        self.assertIn("<b>Sports</b>", str(captured[0]["text"]))
        self.assertIn("last runtime line", str(captured[0]["text"]))

    def test_telegram_health_notifier_sends_error_and_recovery(self) -> None:
        sent_messages: list[str] = []
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                repo_root=Path(temp_dir),
                logs_root=Path("logs"),
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_health_enabled=True,
                telegram_health_cooldown_seconds=3600,
            )
            notifier = TelegramNotifier(settings, send_func=sent_messages.append)
            first = notifier.notify_health(
                {
                    "sxbet": {
                        "status": "error",
                        "market_count": 0,
                        "snapshot_path": "data/ingest/sxbet/x.jsonl.gz",
                        "error": "connection reset",
                    }
                },
                "monitor",
            )
            second = notifier.notify_health(
                {
                    "sxbet": {
                        "status": "ok",
                        "market_count": 40,
                        "snapshot_path": "data/ingest/sxbet/y.jsonl.gz",
                    }
                },
                "monitor",
            )

        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        self.assertEqual(len(sent_messages), 2)
        self.assertIn("[HEALTH][MONITOR] SXBET ERROR", sent_messages[0])
        self.assertIn("connection reset", sent_messages[0])
        self.assertIn("[HEALTH][MONITOR] SXBET OK", sent_messages[1])
        self.assertIn("Recovered from: ERROR", sent_messages[1])

    def test_telegram_debug_cycle_sends_market_batches_and_matches(self) -> None:
        markets = normalize_snapshots(
            [
                load_snapshot("polymarket_btc_100k.json"),
                load_snapshot("myriad_btc_100k.json"),
                load_snapshot("azuro_lakers_game.json"),
            ],
            self.settings,
        )
        matched_pairs, canonical_events = MarketMatcher(self.settings).match(markets)
        self.assertEqual(len(matched_pairs), 1)

        sent_messages: list[str] = []
        settings = Settings(
            repo_root=Path.cwd(),
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_debug_markets_enabled=True,
            telegram_debug_matches_enabled=True,
            telegram_debug_markets_per_exchange=1,
            telegram_debug_batch_size=1,
            telegram_debug_match_limit=1,
        )
        notifier = TelegramNotifier(settings, send_func=sent_messages.append)
        sent = notifier.notify_debug_cycle(markets, matched_pairs, canonical_events, "monitor")

        self.assertEqual(sent, 4)
        self.assertEqual(len(sent_messages), 4)
        self.assertIn("[DEBUG][MONITOR][POLYMARKET][MARKETS]", sent_messages[0])
        self.assertIn("[DEBUG][MONITOR][AZURO][MARKETS]", sent_messages[1])
        self.assertIn("[DEBUG][MONITOR][MYRIAD][MARKETS]", sent_messages[2])
        self.assertIn("[DEBUG][MONITOR][MATCHES]", sent_messages[3])
        self.assertIn("BTC", sent_messages[3])

    def test_sports_normalize_detects_esports_best_of(self) -> None:
        market = normalize_sports_snapshots(
            [
                make_myriad_snapshot(
                    "9001",
                    "Team Spirit vs NAVI bo3",
                    [("Team Spirit", "0.47"), ("NAVI", "0.49")],
                    "2026-03-11T18:00:00Z",
                    topics=["esports", "counter-strike"],
                    description="Counter-Strike best of 3 series",
                )
            ],
            self.settings,
        )[0]
        self.assertEqual(market.sport_key, "esports")
        self.assertTrue(market.is_esports)
        self.assertEqual(market.best_of, 3)
        self.assertEqual(market.market_family, "moneyline_2way")
        self.assertTrue(market.auto_arb_enabled)

    def test_sports_normalize_opn_builds_match_market(self) -> None:
        market = normalize_sports_snapshots(
            [
                make_opn_web_snapshot(
                    "nba-bulls-vs-clippers-mar-13-10-30pm-et",
                    "NBA: Bulls vs Clippers (Mar. 13 10:30PM ET)",
                    "Bulls",
                    "Clippers",
                    "0.20",
                    "0.824",
                    "1773360000",
                    labels=["NBA", "Sports"],
                )
            ],
            self.settings,
        )[0]
        self.assertEqual(market.exchange, "opn")
        self.assertEqual(market.sport_key, "basketball")
        self.assertEqual(market.market_family, "moneyline_2way")
        self.assertEqual(market.participants_home, "Bulls")
        self.assertEqual(market.participants_away, "Clippers")
        self.assertEqual(market.features["underlying_event_title"], "Bulls vs Clippers")
        self.assertIsNotNone(market.features["strict_event_key"])
        self.assertIsNotNone(market.features["soft_event_key"])

    def test_sports_normalize_kalshi_builds_match_market(self) -> None:
        market = normalize_sports_snapshots(
            [make_kalshi_snapshot("KXNBAGAME-26MAR11CHASAC-CHA", "Charlotte at Sacramento Winner?", "Charlotte", "Sacramento", "0.87", "0.14", "2026-03-26T02:00:00Z")],
            self.settings,
        )[0]
        self.assertEqual(market.exchange, "kalshi")
        self.assertEqual(market.sport_key, "basketball")
        self.assertEqual(market.market_family, "moneyline_2way")
        self.assertEqual(market.participants_home, "Charlotte")
        self.assertEqual(market.participants_away, "Sacramento")

    def test_sports_matcher_links_same_match_across_venues(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_polymarket_match_snapshot("pm-1", "Will James Dickens beat Anthony Cacace?", "0.45", "0.55", "2026-03-10T20:00:00Z"),
                make_azuro_match_snapshot("az-1", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"]),
            ],
            self.settings,
        )
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)
        self.assertEqual(len({market.exchange for market in canonical_events[0].markets}), 2)

    def test_sports_matcher_links_same_underlying_event_across_market_families(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_opn_web_snapshot(
                    "nba-lakers-vs-rockets-mar-16-9-30pm-et",
                    "NBA: Lakers vs Rockets (Mar. 16 9:30PM ET)",
                    "Over 224.5",
                    "Under 224.5",
                    "0.51",
                    "0.49",
                    "2026-03-16T21:30:00Z",
                    labels=["NBA", "Sports"],
                ),
                make_azuro_match_snapshot(
                    "az-evt-1",
                    "Lakers - Rockets",
                    ["Lakers", "Rockets"],
                    ["1.95", "1.95"],
                    starts_at="2026-03-16T21:30:00Z",
                    sport_name="Basketball",
                ),
            ],
            self.settings,
        )
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)
        self.assertEqual(canonical_events[0].title, "Lakers vs Rockets")

    def test_sports_matcher_links_city_team_names_to_short_nicknames(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_opn_web_snapshot(
                    "nba-los-angeles-lakers-vs-boston-celtics-mar-18-7-30pm-et",
                    "NBA: Los Angeles Lakers vs Boston Celtics (Mar. 18 7:30PM ET)",
                    "Over 224.5",
                    "Under 224.5",
                    "0.51",
                    "0.49",
                    "2026-03-18T19:30:00Z",
                    labels=["NBA", "Sports"],
                ),
                make_azuro_match_snapshot(
                    "az-city-1",
                    "Lakers - Celtics",
                    ["Lakers", "Celtics"],
                    ["1.95", "1.95"],
                    starts_at="2026-03-18T19:30:00Z",
                    sport_name="Basketball",
                ),
            ],
            self.settings,
        )
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)
        self.assertIn("Lakers", canonical_events[0].title)
        self.assertIn("Celtics", canonical_events[0].title)

    def test_sports_matcher_links_abbreviated_team_names_to_full_names(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_opn_web_snapshot(
                    "ncaab-ohio-st-vs-michigan-mar-20-7-00pm-et",
                    "Basketball: Ohio St vs Michigan (Mar. 20 7:00PM ET)",
                    "Ohio St",
                    "Michigan",
                    "0.46",
                    "0.54",
                    "2026-03-20T19:00:00Z",
                    labels=["Basketball", "Sports"],
                ),
                make_azuro_match_snapshot(
                    "az-abbrev-1",
                    "Ohio State - Michigan",
                    ["Ohio State", "Michigan"],
                    ["1.90", "1.90"],
                    starts_at="2026-03-20T19:00:00Z",
                    sport_name="Basketball",
                ),
            ],
            self.settings,
        )
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)

    def test_sports_matcher_clusters_same_event_across_three_venues(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_opn_web_snapshot(
                    "ncaab-ohio-st-vs-michigan-mar-20-7-00pm-et",
                    "Basketball: Ohio St vs Michigan (Mar. 20 7:00PM ET)",
                    "Ohio St",
                    "Michigan",
                    "0.46",
                    "0.54",
                    "2026-03-20T19:00:00Z",
                    labels=["Basketball", "Sports"],
                ),
                make_azuro_match_snapshot(
                    "az-abbrev-1",
                    "Ohio State - Michigan",
                    ["Ohio State", "Michigan"],
                    ["1.90", "1.90"],
                    starts_at="2026-03-20T19:00:00Z",
                    sport_name="Basketball",
                ),
                make_kalshi_snapshot(
                    "KXNCAAB-26MAR20OHSTMICH-OSU",
                    "Ohio State at Michigan Winner?",
                    "Ohio State",
                    "Michigan",
                    "0.47",
                    "0.55",
                    "2026-03-20T19:00:00Z",
                    event_ticker="KXNCAAB-26MAR20OHSTMICH",
                    rules_primary="If Ohio State wins the college basketball game against Michigan, then the market resolves to Yes.",
                ),
            ],
            self.settings,
        )
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 3)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)
        self.assertEqual(len({market.exchange for market in canonical_events[0].markets}), 3)

    def test_sports_matcher_links_kalshi_match_when_subtitles_duplicate(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_azuro_match_snapshot(
                    "az-kx-1",
                    "James Dickens - Anthony Cacace",
                    ["James Dickens", "Anthony Cacace"],
                    ["2.50", "1.80"],
                    starts_at="2026-03-12T20:00:00Z",
                ),
                make_kalshi_snapshot(
                    "KXBOXING-26MAR12DICKCAC-DICK",
                    "Will James Dickens beat Anthony Cacace in their boxing match?",
                    "James Dickens",
                    "James Dickens",
                    "0.62",
                    "0.41",
                    "2026-03-10T20:00:00Z",
                    event_ticker="KXBOXING-26MAR12DICKCAC",
                    rules_primary="If James Dickens wins the professional boxing match against Anthony Cacace, then the market resolves to Yes.",
                ),
            ],
            self.settings,
        )
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)

    def test_sports_matcher_rejects_match_vs_future(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_azuro_match_snapshot("az-2", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"]),
                make_myriad_snapshot(
                    "9010",
                    "Who will win Professional Boxing League 2026?",
                    [("James Dickens", "0.30"), ("Anthony Cacace", "0.40"), ("Other", "0.30")],
                    "2026-08-01T18:00:00Z",
                    category="boxing",
                    topics=["boxing", "professional boxing league"],
                ),
            ],
            self.settings,
        )
        _, event_links, _ = SportsEventMatcher(self.settings).link(markets)
        self.assertFalse(any(link["status"] == "linked" for link in event_links))

    def test_sports_matcher_links_same_future_by_title(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_opn_snapshot(
                    "opn-future-1",
                    "CS2 ESL Pro League S23 Winner",
                    [("Team Spirit", "0.42"), ("FaZe Clan", "0.58"), ("Other", "0.12")],
                    "2026-03-15T00:00:00Z",
                    category="esports",
                ),
                make_myriad_snapshot(
                    "9901",
                    "Who will win CS2 ESL Pro League Season 23?",
                    [("Team Spirit", "0.40"), ("FaZe Clan", "0.60"), ("Other", "0.10")],
                    "2026-03-16T00:00:00Z",
                    category="esports",
                    topics=["cs2", "esl pro league", "season 23"],
                ),
            ],
            self.settings,
        )
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)

    def test_sports_scanner_finds_cover_opportunity_for_two_way_match(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_polymarket_match_snapshot("pm-3", "Will James Dickens beat Anthony Cacace?", "0.55", "0.55", "2026-03-10T20:00:00Z"),
                make_azuro_match_snapshot("az-3", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.67"]),
            ],
            self.settings,
        )
        canonical_events, event_links, _ = SportsEventMatcher(self.settings).link(markets)
        linked_events = [event for event in canonical_events if len({market.exchange for market in event.markets}) >= 2]
        self.assertEqual(len([link for link in event_links if link["status"] == "linked"]), 2)
        opportunities = SportsArbitrageScanner(self.settings).scan(linked_events)
        self.assertTrue(opportunities)
        self.assertGreater(opportunities[0].profit_pct, 0.0)

    def test_sports_scanner_ignores_different_line_values(self) -> None:
        markets = normalize_sports_snapshots(
            [
                make_myriad_snapshot(
                    "9020",
                    "Lakers vs Celtics total 210.5",
                    [("Over 210.5", "0.48"), ("Under 210.5", "0.49")],
                    "2026-03-12T18:00:00Z",
                    category="basketball",
                    topics=["basketball", "nba"],
                ),
                make_azuro_total_snapshot("az-total-1", "Lakers - Celtics", ["Lakers", "Celtics"], "212.5"),
            ],
            self.settings,
        )
        canonical_events, _, _ = SportsEventMatcher(self.settings).link(markets)
        opportunities = SportsArbitrageScanner(self.settings).scan(canonical_events)
        self.assertEqual(opportunities, [])

    def test_sports_cache_marks_stale_and_loads_aliases(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                repo_root=Path(temp_dir),
                sports_db_path=Path("data/sports_cache.sqlite3"),
            )
            cache = SportsCache(settings)
            try:
                first_markets = normalize_sports_snapshots(
                    [
                        make_azuro_match_snapshot("az-cache-1", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"]),
                        make_azuro_match_snapshot("az-cache-2", "Lakers - Celtics", ["Lakers", "Celtics"], ["1.90", "1.90"], sport_name="Basketball"),
                    ],
                    settings,
                )
                first_snapshots = [
                    make_azuro_match_snapshot("az-cache-1", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"]),
                    make_azuro_match_snapshot("az-cache-2", "Lakers - Celtics", ["Lakers", "Celtics"], ["1.90", "1.90"], sport_name="Basketball"),
                ]
                cache.upsert_discovery("azuro", Path(temp_dir) / "first.jsonl.gz", first_snapshots, first_markets)
                second_markets = normalize_sports_snapshots(
                    [make_azuro_match_snapshot("az-cache-1", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"])],
                    settings,
                )
                second_snapshots = [make_azuro_match_snapshot("az-cache-1", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"])]
                cache.upsert_discovery("azuro", Path(temp_dir) / "second.jsonl.gz", second_snapshots, second_markets)
                counts = cache.counts()
                self.assertEqual(counts["venue_markets"], 1)
                cache.connection.execute(
                    "INSERT INTO aliases (alias_type, sport_key, source_value, canonical_value) VALUES (?, ?, ?, ?)",
                    ("participant", "boxing", "james dickens", "jim dickens"),
                )
                cache.connection.commit()
                aliases = cache.load_aliases()
                self.assertEqual(aliases[("participant", "boxing:james dickens")], "jim dickens")
            finally:
                cache.close()

    def test_telegram_send_text_swallows_network_errors(self) -> None:
        settings = Settings(repo_root=Path.cwd(), telegram_bot_token="token", telegram_chat_id="123")
        notifier = TelegramNotifier(
            settings,
            send_func=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        notifier.send_text("hello")

    def test_normalize_sxbet_builds_binary_market_from_orders(self) -> None:
        market = normalize_snapshots(
            [make_sxbet_snapshot("sx-1", "Atletico Madrid", "Tottenham Hotspur", "1773172800")],
            self.settings,
        )[0]
        self.assertEqual(market.exchange, "sxbet")
        self.assertEqual(market.market_type, "binary")
        self.assertEqual(market.category_family, "sports")
        self.assertEqual(len(market.outcomes), 2)
        self.assertAlmostEqual(market.outcomes[0].quote.best_ask or 0.0, 0.45, places=3)

    def test_matcher_links_binary_yes_no_with_entity_vs_other_market(self) -> None:
        snapshots = [
            make_snapshot(
                "polymarket",
                "pm-award-1",
                {
                    "question": "Will One Battle After Another win Best Picture at the 98th Academy Awards?",
                    "description": (
                        "This market will resolve according to the listed movie that wins the 98th Academy Award for "
                        "Best Picture. If no winner is declared by June 30, 2026, this market will resolve to Other."
                    ),
                    "endDate": "2026-06-30T23:59:00Z",
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.31", "0.69"]),
                    "clobTokenIds": json.dumps(["pm-award-1-yes", "pm-award-1-no"]),
                    "orderBooks": {
                        "pm-award-1-yes": {"asks": [{"price": "0.31", "size": "300"}], "bids": [{"price": "0.30", "size": "300"}]},
                        "pm-award-1-no": {"asks": [{"price": "0.69", "size": "300"}], "bids": [{"price": "0.68", "size": "300"}]},
                    },
                    "volumeNum": "12000",
                    "liquidityNum": "7000",
                },
            ),
            make_sxbet_snapshot(
                "sx-award-1",
                "One Battle After Another",
                "Other",
                "1773615600",
                sport_label="Entertainment",
                league_label="Academy Awards - Best Picture",
                group1="Academy Awards - Best Picture",
                group2="Entertainment",
            ),
        ]
        markets = normalize_snapshots(snapshots, self.settings)
        matched_pairs, _ = MarketMatcher(self.settings).match(markets)

        self.assertEqual(len(matched_pairs), 1)
        pair = matched_pairs[0]
        self.assertGreaterEqual(pair.confidence, self.settings.min_match_confidence)
        self.assertEqual(pair.outcome_mapping["Yes"], "One Battle After Another")
        self.assertEqual(pair.outcome_mapping["No"], "Other")

    def test_normalize_opn_uses_outcome_prices(self) -> None:
        market = normalize_snapshots(
            [make_opn_snapshot("opn-1", "Will BTC hit $150k by Dec 2026?", [("Yes", "0.42"), ("No", "0.58")], "2026-12-31T00:00:00Z")],
            self.settings,
        )[0]
        self.assertEqual(market.exchange, "opn")
        self.assertEqual(market.market_type, "binary")
        self.assertAlmostEqual(market.outcomes[0].quote.best_ask or 0.0, 0.42, places=3)

    def test_normalize_opn_api_market_title_is_used_for_sports_event(self) -> None:
        snapshot = make_snapshot(
            "opn",
            "9626",
            {
                "marketId": 9626,
                "marketTitle": "JDG vs AG (KPL Spring) - Mar 18 8:00 AM ET",
                "yesLabel": "JDG",
                "noLabel": "AG",
                "cutoffAt": "2026-03-18T12:00:00Z",
                "volume": "1500",
                "chainId": "56",
                "quoteToken": "USDT",
            },
        )

        market = normalize_snapshots([snapshot], self.settings)[0]
        self.assertEqual(market.title, "JDG vs AG (KPL Spring) - Mar 18 8:00 AM ET")
        self.assertEqual(market.closes_at.isoformat(), "2026-03-18T12:00:00+00:00")

        sports_market = normalize_sports_snapshots([snapshot], self.settings)[0]
        self.assertEqual(sports_market.sport_key, "esports")
        self.assertEqual(sports_market.participants_sorted, ("ag", "jdg"))
        self.assertEqual(sports_market.title, "JDG vs AG (KPL Spring) - Mar 18 8:00 AM ET")

    def test_sports_normalize_ignores_opn_crypto_api_market(self) -> None:
        snapshot = make_snapshot(
            "opn",
            "9556",
            {
                "marketId": 9556,
                "marketTitle": "Bitcoin Up or Down on March 16?(12:00 ET)",
                "yesLabel": "UP",
                "noLabel": "DOWN",
                "labels": ["Crypto"],
                "cutoffAt": "2026-03-16T16:00:00Z",
                "rules": 'The resolution source for this market is Binance, specifically the BTC/USDT "Close" prices.',
            },
        )

        self.assertEqual(normalize_sports_snapshots([snapshot], self.settings), [])

    def test_normalize_opn_web_topic_uses_buy_prices(self) -> None:
        market = normalize_snapshots(
            [
                make_opn_web_snapshot(
                    "nba-bulls-vs-clippers-mar-13-10-30pm-et",
                    "NBA: Bulls vs Clippers (Mar. 13 10:30PM ET)",
                    "Bulls",
                    "Clippers",
                    "0.20",
                    "0.824",
                    "1773360000",
                    labels=["NBA", "Sports"],
                )
            ],
            self.settings,
        )[0]
        self.assertEqual(market.exchange, "opn")
        self.assertEqual(market.market_type, "binary")
        self.assertEqual(market.category_family, "sports")
        self.assertAlmostEqual(market.outcomes[0].quote.best_ask or 0.0, 0.20, places=3)
        self.assertAlmostEqual(market.outcomes[1].quote.best_ask or 0.0, 0.824, places=3)

    def test_normalize_opn_web_topic_uses_child_list_for_multi_outcome(self) -> None:
        market = normalize_snapshots(
            [
                make_opn_web_child_snapshot(
                    "next-uk-prime-minister-in-2026",
                    "Next UK Prime Minister in 2026?",
                    [("Lucy Powell", "0.965"), ("Angela Rayner", "0.99"), ("No Next PM in 2026", "0.562")],
                    "1798675200",
                )
            ],
            self.settings,
        )[0]
        self.assertEqual(market.exchange, "opn")
        self.assertEqual(market.market_type, "multi_outcome")
        self.assertEqual(len(market.outcomes), 3)
        self.assertAlmostEqual(market.outcomes[0].quote.best_ask or 0.0, 0.965, places=3)

    def test_normalize_kalshi_uses_top_of_book_prices(self) -> None:
        market = normalize_snapshots(
            [make_kalshi_snapshot("KXNBAGAME-26MAR11CHASAC-CHA", "Charlotte at Sacramento Winner?", "Charlotte", "Sacramento", "0.87", "0.14", "2026-03-26T02:00:00Z")],
            self.settings,
        )[0]
        self.assertEqual(market.exchange, "kalshi")
        self.assertEqual(market.market_type, "binary")
        self.assertEqual(market.category_family, "sports")
        self.assertAlmostEqual(market.outcomes[0].quote.best_ask or 0.0, 0.87, places=3)
        self.assertAlmostEqual(market.outcomes[0].quote.best_bid or 0.0, 0.86, places=3)

    def test_normalize_kalshi_duplicate_subtitles_falls_back_to_yes_no_labels(self) -> None:
        market = normalize_snapshots(
            [
                make_kalshi_snapshot(
                    "KXBOXING-26MAR12DICKCAC-DICK",
                    "Will James Dickens beat Anthony Cacace in their boxing match?",
                    "James Dickens",
                    "James Dickens",
                    "0.62",
                    "0.41",
                    "2026-03-26T20:00:00Z",
                    event_ticker="KXBOXING-26MAR12DICKCAC",
                    rules_primary="If James Dickens wins the professional boxing match against Anthony Cacace, then the market resolves to Yes.",
                )
            ],
            self.settings,
        )[0]
        self.assertEqual(market.outcomes[0].label, "Yes")
        self.assertEqual(market.outcomes[1].label, "No")
        self.assertEqual(market.features.get("subject_entity"), "james dickens")
        self.assertEqual(market.features.get("market_lane"), "binary_subject")
        sports_market = normalize_sports_snapshots(
            [
                make_kalshi_snapshot(
                    "KXBOXING-26MAR12DICKCAC-DICK",
                    "Will James Dickens beat Anthony Cacace in their boxing match?",
                    "James Dickens",
                    "James Dickens",
                    "0.62",
                    "0.41",
                    "2026-03-26T20:00:00Z",
                    event_ticker="KXBOXING-26MAR12DICKCAC",
                    rules_primary="If James Dickens wins the professional boxing match against Anthony Cacace, then the market resolves to Yes.",
                )
            ],
            self.settings,
        )[0]
        self.assertEqual(sports_market.participants_home, "James Dickens")
        self.assertEqual(sports_market.participants_away, "Anthony Cacace")
        self.assertEqual(sports_market.start_time.date().isoformat(), "2026-03-12")

    def test_frontier_matching_reaches_locked_markets_for_new_exchange(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                repo_root=Path(temp_dir),
                market_db_path=Path("data/market_cache.sqlite3"),
                min_profit_pct=1.0,
                quote_ttl_seconds=400_000_000,
            )
            cache = MarketCache(settings)
            try:
                base_snapshots = [
                    make_snapshot(
                        "polymarket",
                        "pm-award-locked",
                        {
                            "question": "Will Teyana Taylor win Best Supporting Actress at the 98th Academy Awards?",
                            "description": "This market resolves to Yes if Teyana Taylor wins Best Supporting Actress at the 98th Academy Awards, otherwise No.",
                            "endDate": "2026-06-30T23:59:00Z",
                            "outcomes": json.dumps(["Yes", "No"]),
                            "outcomePrices": json.dumps(["0.31", "0.69"]),
                            "clobTokenIds": json.dumps(["pm-award-locked-yes", "pm-award-locked-no"]),
                            "orderBooks": {
                                "pm-award-locked-yes": {"asks": [{"price": "0.31", "size": "300"}], "bids": [{"price": "0.30", "size": "300"}]},
                                "pm-award-locked-no": {"asks": [{"price": "0.69", "size": "300"}], "bids": [{"price": "0.68", "size": "300"}]},
                            },
                            "volumeNum": "12000",
                            "liquidityNum": "7000",
                        },
                    ),
                    make_sxbet_snapshot(
                        "sx-award-locked",
                        "Teyana Taylor",
                        "Other",
                        "1773615600",
                        sport_label="Entertainment",
                        league_label="Academy Awards - Best Supporting Actress",
                        group1="Academy Awards - Best Supporting Actress",
                        group2="Entertainment",
                    ),
                ]
                base_markets = normalize_snapshots(base_snapshots, settings)
                cache.upsert_discovery("polymarket", Path(temp_dir) / "pm.jsonl.gz", [base_snapshots[0]], [base_markets[0]])
                cache.upsert_discovery("sxbet", Path(temp_dir) / "sx.jsonl.gz", [base_snapshots[1]], [base_markets[1]])
                initial_pairs, _ = MarketMatcher(settings).match(cache.load_unmatched_markets())
                stored_pairs = cache.store_pairs(initial_pairs)
                self.assertEqual(len(stored_pairs), 1)

                kalshi_snapshot = make_snapshot(
                    "kalshi",
                    "KXOSCARS-26TAYLOR",
                    {
                        "ticker": "KXOSCARS-26TAYLOR",
                        "event_ticker": "KXOSCARS-26BESTSUPPACTRESS",
                        "title": "Will Teyana Taylor win Best Supporting Actress at the 98th Academy Awards?",
                        "rules_primary": "If Teyana Taylor wins Best Supporting Actress at the 98th Academy Awards, then the market resolves to Yes.",
                        "yes_sub_title": "Teyana Taylor",
                        "no_sub_title": "Teyana Taylor",
                        "yes_ask_dollars": "0.34",
                        "yes_bid_dollars": "0.33",
                        "yes_ask_size_fp": "1000.00",
                        "no_ask_dollars": "0.66",
                        "no_bid_dollars": "0.64",
                        "no_ask_size_fp": "1000.00",
                        "volume_fp": "1000.00",
                        "liquidity_dollars": "1000.00",
                        "close_time": "2026-06-30T23:59:00Z",
                        "expiration_time": "2026-06-30T23:59:00Z",
                        "market_type": "binary",
                        "status": "active",
                    },
                )
                kalshi_market = normalize_snapshots([kalshi_snapshot], settings)[0]
                frontier = cache.upsert_discovery("kalshi", Path(temp_dir) / "kalshi.jsonl.gz", [kalshi_snapshot], [kalshi_market])
                active_markets = cache.load_active_markets()
                new_pairs, _ = MarketMatcher(settings).match_frontier(frontier, active_markets)

                self.assertTrue(new_pairs)
                self.assertTrue(
                    any(
                        {"kalshi", "polymarket"} == {pair.market_a.exchange, pair.market_b.exchange}
                        for pair in new_pairs
                    )
                )
            finally:
                cache.close()

    def test_market_cache_locks_matched_markets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                repo_root=Path(temp_dir),
                market_db_path=Path("data/market_cache.sqlite3"),
            )
            cache = MarketCache(settings)
            try:
                snapshots = [
                    make_polymarket_match_snapshot("pm-cache-1", "Will James Dickens beat Anthony Cacace?", "0.45", "0.55", "2026-03-10T20:00:00Z"),
                    make_azuro_match_snapshot("az-cache-1", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"]),
                    make_opn_snapshot("opn-cache-1", "Will BTC hit $150k by Dec 2026?", [("Yes", "0.42"), ("No", "0.58")], "2026-12-31T00:00:00Z"),
                ]
                markets = normalize_snapshots(snapshots, settings)
                cache.upsert_discovery("polymarket", Path(temp_dir) / "pm.jsonl.gz", [snapshots[0]], [markets[0]])
                cache.upsert_discovery("azuro", Path(temp_dir) / "az.jsonl.gz", [snapshots[1]], [markets[1]])
                cache.upsert_discovery("opn", Path(temp_dir) / "opn.jsonl.gz", [snapshots[2]], [markets[2]])
                matcher = MarketMatcher(settings)
                pairs, _ = matcher.match(cache.load_unmatched_markets())
                stored = cache.store_pairs(pairs)
                self.assertEqual(len(stored), 1)
                remaining = cache.load_unmatched_markets()
                remaining_keys = {(market.exchange, market.market_id) for market in remaining}
                self.assertNotIn(("polymarket", "pm-cache-1"), remaining_keys)
                self.assertNotIn(("azuro", "az-cache-1"), remaining_keys)
                self.assertIn(("opn", "opn-cache-1"), remaining_keys)
            finally:
                cache.close()

    def test_market_cache_refresh_lifecycle_unlocks_inactive_pair(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = Settings(
                repo_root=Path(temp_dir),
                market_db_path=Path("data/market_cache.sqlite3"),
                quote_ttl_seconds=400_000_000,
            )
            cache = MarketCache(settings)
            try:
                snapshots = [
                    make_polymarket_match_snapshot("pm-life-1", "Will James Dickens beat Anthony Cacace?", "0.45", "0.55", "2026-03-10T20:00:00Z"),
                    make_azuro_match_snapshot("az-life-1", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], ["2.50", "1.80"]),
                ]
                markets = normalize_snapshots(snapshots, settings)
                cache.upsert_discovery("polymarket", Path(temp_dir) / "pm.jsonl.gz", [snapshots[0]], [markets[0]])
                cache.upsert_discovery("azuro", Path(temp_dir) / "az.jsonl.gz", [snapshots[1]], [markets[1]])
                pairs, _ = MarketMatcher(settings).match(cache.load_unmatched_markets())
                cache.store_pairs(pairs)

                cache.upsert_discovery("azuro", Path(temp_dir) / "az-empty.jsonl.gz", [], [])
                cache.refresh_lifecycle()

                self.assertEqual(cache.load_pairs(), [])
                remaining_keys = {(market.exchange, market.market_id) for market in cache.load_unmatched_markets()}
                self.assertIn(("polymarket", "pm-life-1"), remaining_keys)
            finally:
                cache.close()

    def test_matcher_rejects_same_event_with_different_resolution_scope(self) -> None:
        snapshots = [
            make_snapshot(
                "polymarket",
                "pm-btc-expiry",
                {
                    "question": "Will Bitcoin hit $100,000 by March 31, 2026?",
                    "description": "This market resolves to Yes if BTC/USD touches $100,000 before March 31, 2026.",
                    "endDate": "2026-03-31T23:59:00Z",
                    "outcomes": json.dumps(["Yes", "No"]),
                    "outcomePrices": json.dumps(["0.40", "0.60"]),
                    "clobTokenIds": json.dumps(["pm-btc-expiry-yes", "pm-btc-expiry-no"]),
                    "orderBooks": {
                        "pm-btc-expiry-yes": {"asks": [{"price": "0.40", "size": "300"}], "bids": [{"price": "0.39", "size": "300"}]},
                        "pm-btc-expiry-no": {"asks": [{"price": "0.60", "size": "300"}], "bids": [{"price": "0.59", "size": "300"}]},
                    },
                    "volumeNum": "12000",
                    "liquidityNum": "7000",
                },
            ),
            make_snapshot(
                "opn",
                "opn-btc-hourly",
                {
                    "marketId": "opn-btc-hourly",
                    "marketTitle": "Bitcoin above $100,000 on Mar 31, 2026 12:00 UTC?",
                    "description": "Hourly cutoff market. Resolves to YES if BTC is above $100,000 at the hourly close.",
                    "cutoffAt": "2026-03-31T12:00:00Z",
                    "yesLabel": "Yes",
                    "noLabel": "No",
                    "yesBuyPrice": "0.41",
                    "noBuyPrice": "0.59",
                    "labels": ["Crypto"],
                },
            ),
        ]
        markets = normalize_snapshots(snapshots, self.settings)
        matched_pairs, _ = MarketMatcher(self.settings).match(markets)
        self.assertEqual(matched_pairs, [])

    def test_matcher_rejects_same_sports_event_with_different_market_family(self) -> None:
        snapshots = [
            make_polymarket_match_snapshot("pm-sport-family", "Will James Dickens beat Anthony Cacace?", "0.55", "0.55", "2026-03-10T20:00:00Z"),
            make_azuro_total_snapshot("az-sport-family", "James Dickens - Anthony Cacace", ["James Dickens", "Anthony Cacace"], "8.5"),
        ]
        markets = normalize_snapshots(snapshots, self.settings)
        matched_pairs, _ = MarketMatcher(self.settings).match(markets)
        self.assertEqual(matched_pairs, [])

    def test_sports_matcher_links_missing_date_fixture_via_fallback_identity(self) -> None:
        snapshots = [
            make_opn_web_snapshot(
                "nba-lakers-vs-celtics-no-date",
                "NBA: Lakers vs Celtics",
                "Lakers",
                "Celtics",
                "0.45",
                "0.55",
                "",
                labels=["NBA", "Sports"],
            ),
            make_snapshot(
                "azuro",
                "az-no-date-1",
                {
                    "environment": "PolygonUSDT",
                    "game": {
                        "gameId": "game-az-no-date-1",
                        "title": "Lakers - Celtics",
                        "startsAt": "",
                        "turnover": "8000",
                        "sport": {"name": "Basketball", "slug": "basketball"},
                        "league": {"name": "NBA", "slug": "nba"},
                        "participants": [{"name": "Lakers"}, {"name": "Celtics"}],
                    },
                    "condition": {
                        "conditionId": "az-no-date-1",
                        "margin": 0,
                        "outcomes": [
                            {"outcomeId": "az-no-date-1-1", "odds": "1.90"},
                            {"outcomeId": "az-no-date-1-2", "odds": "1.90"},
                        ],
                        "game": {"gameId": "game-az-no-date-1"},
                    },
                },
            ),
        ]
        markets = normalize_sports_snapshots(snapshots, self.settings)
        canonical_events, event_links, review_items = SportsEventMatcher(self.settings).link(markets)
        linked = [link for link in event_links if link["status"] == "linked"]
        self.assertEqual(len(linked), 2)
        self.assertEqual(len(review_items), 0)
        self.assertEqual(len(canonical_events), 1)


if __name__ == "__main__":
    unittest.main()
