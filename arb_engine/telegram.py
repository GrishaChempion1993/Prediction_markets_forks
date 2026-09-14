from __future__ import annotations

from collections import defaultdict
from html import escape
from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

from .config import Settings
from .models import ArbitrageOpportunity, CanonicalEvent, MatchedPair, NormalizedMarket
from .utils import ensure_parent, text_hash, utc_now


SendFunc = Callable[[str], None]


class TelegramNotifier:
    API_RETRY_DELAYS_SECONDS = (0, 1, 3)

    def __init__(
        self,
        settings: Settings,
        send_func: SendFunc | None = None,
        now_func: Callable[[], object] | None = None,
    ) -> None:
        self.settings = settings
        self.send_func = send_func
        self.now_func = now_func or utc_now
        self.state_path = settings.repo_root / settings.logs_root / "telegram_state.json"
        self._lock = Lock()
        self._state = self._read_state()
        self._stop_event = Event()
        self._listener_thread: Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def start_listener(self) -> None:
        if not self.enabled or self.send_func is not None:
            return
        if self._listener_thread and self._listener_thread.is_alive():
            return
        self._listener_thread = Thread(target=self._listener_loop, name="telegram-listener", daemon=True)
        self._listener_thread.start()

    def notify_opportunities(self, opportunities: list[ArbitrageOpportunity], mode: str) -> int:
        if not self.enabled:
            return 0

        sent_count = 0
        now = self.now_func()
        with self._lock:
            fingerprints = dict(self._state.get("fingerprints", {}))
        cutoff = now.timestamp() - self.settings.telegram_cooldown_seconds
        fingerprints = {fingerprint: ts for fingerprint, ts in fingerprints.items() if ts >= cutoff}

        for opportunity in opportunities:
            if opportunity.status not in self.settings.telegram_statuses:
                continue
            if opportunity.profit_pct < self.settings.telegram_min_profit_pct:
                continue
            fingerprint = self._fingerprint(opportunity)
            if fingerprint in fingerprints:
                continue
            try:
                self._deliver_message(self._format_opportunity_message(opportunity, mode), self._delete_button_markup())
            except Exception as error:  # pragma: no cover - network/runtime fallback
                print(f"telegram_send_failed error={error}")
                continue
            fingerprints[fingerprint] = now.timestamp()
            sent_count += 1

        with self._lock:
            self._state["fingerprints"] = fingerprints
            self._save_state_unlocked()
        return sent_count

    def send_test_message(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._deliver_message(text, self._delete_button_markup())
        except Exception as error:  # pragma: no cover - network/runtime fallback
            print(f"telegram_send_failed error={error}")

    def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._deliver_message(text, self._delete_button_markup())
        except Exception as error:  # pragma: no cover - network/runtime fallback
            print(f"telegram_send_failed error={error}")

    def notify_debug_cycle(
        self,
        markets: list[NormalizedMarket],
        matched_pairs: list[MatchedPair],
        canonical_events: list[CanonicalEvent],
        mode: str,
    ) -> int:
        if not self.enabled:
            return 0

        messages: list[str] = []
        if self.settings.telegram_debug_markets_enabled:
            messages.extend(self._build_market_debug_messages(markets, mode))
        if self.settings.telegram_debug_matches_enabled:
            messages.extend(self._build_match_debug_messages(matched_pairs, canonical_events, mode))

        for message in messages:
            try:
                self._deliver_message(message, self._delete_button_markup())
            except Exception as error:  # pragma: no cover - network/runtime fallback
                print(f"telegram_send_failed error={error}")
        return len(messages)

    def notify_health(self, exchange_statuses: dict[str, dict[str, object]], mode: str) -> int:
        if not self.enabled or not self.settings.telegram_health_enabled:
            return 0

        sent_count = 0
        now = self.now_func()
        with self._lock:
            health_state = {str(key): dict(value) for key, value in dict(self._state.get("health", {})).items()}

        for exchange, status in exchange_statuses.items():
            current_status = str(status.get("status") or "unknown")
            previous = dict(health_state.get(exchange, {}))
            previous_status = str(previous.get("status") or "unknown")
            last_alert_at = float(previous.get("last_alert_at") or 0.0)
            should_send = False

            if current_status != "ok":
                should_send = (
                    previous_status != current_status
                    or now.timestamp() - last_alert_at >= self.settings.telegram_health_cooldown_seconds
                )
            elif previous_status in {"error", "empty"}:
                should_send = True

            if should_send:
                try:
                    self._deliver_message(
                        self._format_health_message(exchange, status, previous_status, mode),
                        self._delete_button_markup(),
                    )
                except Exception as error:  # pragma: no cover - network/runtime fallback
                    print(f"telegram_send_failed error={error}")
                else:
                    sent_count += 1
                    last_alert_at = now.timestamp()

            health_state[exchange] = {
                "status": current_status,
                "last_alert_at": last_alert_at,
                "market_count": int(status.get("market_count") or 0),
                "error": str(status.get("error") or ""),
                "snapshot_path": str(status.get("snapshot_path") or ""),
            }

        with self._lock:
            self._state["health"] = health_state
            self._save_state_unlocked()
        return sent_count

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"fingerprints": {}, "health": {}, "update_offset": 0}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"fingerprints": {}, "health": {}, "update_offset": 0}
        if isinstance(payload, dict) and "fingerprints" in payload:
            return {
                "fingerprints": {str(key): float(value) for key, value in dict(payload.get("fingerprints", {})).items()},
                "health": {str(key): dict(value) for key, value in dict(payload.get("health", {})).items()},
                "update_offset": int(payload.get("update_offset", 0)),
            }
        if isinstance(payload, dict):
            return {
                "fingerprints": {str(key): float(value) for key, value in payload.items()},
                "health": {},
                "update_offset": 0,
            }
        return {"fingerprints": {}, "health": {}, "update_offset": 0}

    def _save_state_unlocked(self) -> None:
        ensure_parent(self.state_path)
        self.state_path.write_text(json.dumps(self._state, sort_keys=True), encoding="utf-8")

    def _fingerprint(self, opportunity: ArbitrageOpportunity) -> str:
        legs = "|".join(
            f"{leg.get('exchange')}:{leg.get('market_id')}:{leg.get('buy_outcome')}"
            for leg in opportunity.legs
        )
        return text_hash(
            "|".join(
                [
                    opportunity.strategy,
                    f"{opportunity.market_a.exchange}:{opportunity.market_a.market_id}",
                    f"{opportunity.market_b.exchange}:{opportunity.market_b.market_id}",
                    legs,
                ]
            )
        )

    def _format_opportunity_message(self, opportunity: ArbitrageOpportunity, mode: str) -> str:
        lines = [
            f"[{mode.upper()}] Arbitrage signal",
            f"Strategy: {opportunity.strategy}",
            f"Profit: {opportunity.profit_pct:.2f}%",
            f"Confidence: {opportunity.match_confidence:.2f}",
            f"Status: {opportunity.status}",
            f"A: {opportunity.market_a.exchange} | {opportunity.market_a.title}",
            f"B: {opportunity.market_b.exchange} | {opportunity.market_b.title}",
            "Legs:",
        ]
        for leg in opportunity.legs:
            lines.append(
                f"- {leg.get('exchange')} buy {leg.get('buy_outcome')} "
                f"ask={float(leg.get('best_ask') or 0.0):.4f} cost={float(leg.get('quoted_cost') or 0.0):.2f}"
            )
        lines.extend(
            [
                f"Total cost: {opportunity.cost:.2f}",
                f"Gross payout: {opportunity.gross_payout:.2f}",
                f"Quote age: {opportunity.quote_age_seconds:.0f}s",
            ]
        )
        if opportunity.reason:
            lines.append(f"Reason: {opportunity.reason}")
        return "\n".join(lines)

    def _delete_button_markup(self) -> dict[str, Any]:
        return {"inline_keyboard": [[{"text": "Delete", "callback_data": "delete"}]]}

    def _build_market_debug_messages(self, markets: list[NormalizedMarket], mode: str) -> list[str]:
        grouped: dict[str, list[NormalizedMarket]] = defaultdict(list)
        allowed_exchanges = set(self.settings.telegram_debug_exchanges)
        for market in markets:
            if allowed_exchanges and market.exchange not in allowed_exchanges:
                continue
            grouped[market.exchange].append(market)

        exchange_order = list(self.settings.telegram_debug_exchanges) or list(self.settings.collect_exchanges)
        for exchange in grouped:
            if exchange not in exchange_order:
                exchange_order.append(exchange)

        messages: list[str] = []
        batch_size = max(1, self.settings.telegram_debug_batch_size)
        limit = max(1, self.settings.telegram_debug_markets_per_exchange)
        for exchange in exchange_order:
            exchange_markets = grouped.get(exchange, [])
            if not exchange_markets:
                continue
            ranked = sorted(
                exchange_markets,
                key=lambda market: (market.volume_usd, market.liquidity_usd, market.fetched_at.timestamp()),
                reverse=True,
            )
            sampled = ranked[:limit]
            total = len(ranked)
            for start in range(0, len(sampled), batch_size):
                batch = sampled[start : start + batch_size]
                messages.append(self._format_market_batch_message(exchange, batch, total, start, len(sampled), mode))
        return messages

    def _build_match_debug_messages(
        self,
        matched_pairs: list[MatchedPair],
        canonical_events: list[CanonicalEvent],
        mode: str,
    ) -> list[str]:
        if not matched_pairs:
            if not self.settings.telegram_debug_include_empty_matches:
                return []
            return [
                "\n".join(
                    [
                        f"[DEBUG][{mode.upper()}][MATCHES]",
                        f"matched_pairs=0 canonical_events={len(canonical_events)}",
                        "No matched pairs in this cycle.",
                    ]
                )
            ]

        ranked = sorted(matched_pairs, key=lambda pair: pair.confidence, reverse=True)
        limit = max(1, self.settings.telegram_debug_match_limit)
        batch_size = max(1, self.settings.telegram_debug_batch_size)
        sampled = ranked[:limit]
        messages: list[str] = []
        for start in range(0, len(sampled), batch_size):
            batch = sampled[start : start + batch_size]
            messages.append(self._format_match_batch_message(batch, len(ranked), start, len(sampled), len(canonical_events), mode))
        return messages

    def _format_market_batch_message(
        self,
        exchange: str,
        batch: list[NormalizedMarket],
        total: int,
        start: int,
        sampled_total: int,
        mode: str,
    ) -> str:
        lines = [
            f"[DEBUG][{mode.upper()}][{exchange.upper()}][MARKETS]",
            f"showing {start + 1}-{start + len(batch)} of {sampled_total} sampled, total={total}",
        ]
        for index, market in enumerate(batch, start=start + 1):
            lines.extend(
                [
                    f"{index}. {self._shorten(market.title, 110)}",
                    " | ".join(
                        [
                            f"id={market.market_id}",
                            f"type={market.market_type}",
                            f"kind={market.event_kind}",
                            f"cat={market.category_family}",
                        ]
                    ),
                    " | ".join(
                        [
                            f"close={self._format_datetime(market.closes_at)}",
                            f"vol={self._format_money(market.volume_usd)}",
                            f"liq={self._format_money(market.liquidity_usd)}",
                        ]
                    ),
                    f"outcomes={', '.join(self._shorten(outcome.label, 18) for outcome in market.outcomes)}",
                ]
            )
        return "\n".join(lines)[:3900]

    def _format_match_batch_message(
        self,
        batch: list[MatchedPair],
        total: int,
        start: int,
        sampled_total: int,
        canonical_event_count: int,
        mode: str,
    ) -> str:
        lines = [
            f"[DEBUG][{mode.upper()}][MATCHES]",
            f"showing {start + 1}-{start + len(batch)} of {sampled_total} sampled, total={total}, canonical_events={canonical_event_count}",
        ]
        for index, pair in enumerate(batch, start=start + 1):
            scores = pair.score_breakdown
            lines.extend(
                [
                    f"{index}. conf={pair.confidence:.2f} | {pair.market_a.exchange}<->{pair.market_b.exchange} | {pair.match_method}",
                    f"A: {self._shorten(pair.market_a.title, 100)}",
                    f"B: {self._shorten(pair.market_b.title, 100)}",
                    f"map={self._format_outcome_mapping(pair.outcome_mapping)}",
                    (
                        "scores="
                        f"ent:{scores.get('entities_numeric', 0.0):.2f} "
                        f"res:{scores.get('resolution', 0.0):.2f} "
                        f"text:{scores.get('fuzzy_text', 0.0):.2f} "
                        f"date:{scores.get('date', 0.0):.2f} "
                        f"tag:{scores.get('tags_category', 0.0):.2f} "
                        f"out:{scores.get('outcome_set', 0.0):.2f}"
                    ),
                ]
            )
        return "\n".join(lines)[:3900]

    def _listener_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = self._get_updates(timeout=self.settings.telegram_poll_timeout_seconds)
                for update in updates:
                    self._handle_update(update)
                    with self._lock:
                        self._state["update_offset"] = max(self._state.get("update_offset", 0), int(update["update_id"]) + 1)
                        self._save_state_unlocked()
            except Exception as error:  # pragma: no cover - network/runtime fallback
                print(f"telegram_listener_failed error={error}")
                sleep(self.settings.telegram_poll_interval_seconds)

    def _get_updates(self, timeout: int) -> list[dict[str, Any]]:
        with self._lock:
            offset = int(self._state.get("update_offset", 0))
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        response = self._api_request("getUpdates", payload)
        return list(response.get("result", []))

    def _handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self._handle_message(dict(update["message"]))
        elif "callback_query" in update:
            self._handle_callback(dict(update["callback_query"]))

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self.settings.telegram_chat_id:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        command_parts = text.split()
        command = command_parts[0].split("@", maxsplit=1)[0].lower()
        if command in {"/start", "/help"}:
            body = (
                "Commands:\n"
                "/logs [N] - show latest runtime log lines\n"
                "/status - show scanner summary\n"
                "Debug batches are pushed automatically when enabled in .env"
            )
            self._send_message(body, reply_markup=self._delete_button_markup(), chat_id=chat_id)
        elif command == "/logs":
            line_count = self.settings.telegram_log_lines
            if len(command_parts) > 1:
                try:
                    line_count = max(1, min(int(command_parts[1]), 100))
                except ValueError:
                    line_count = self.settings.telegram_log_lines
            self._send_message(self._format_logs_message(line_count), reply_markup=self._delete_button_markup(), chat_id=chat_id, parse_mode="HTML")
        elif command == "/status":
            self._send_message(self._format_status_message(), reply_markup=self._delete_button_markup(), chat_id=chat_id, parse_mode="HTML")

    def _handle_callback(self, callback_query: dict[str, Any]) -> None:
        callback_id = str(callback_query.get("id") or "")
        data = str(callback_query.get("data") or "")
        message = dict(callback_query.get("message") or {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self.settings.telegram_chat_id:
            self._answer_callback_query(callback_id, "Unauthorized")
            return
        if data == "delete":
            self._delete_message(chat_id, int(message.get("message_id")))
            self._answer_callback_query(callback_id, "Deleted")
        else:
            self._answer_callback_query(callback_id, "Unsupported")

    def _format_logs_message(self, line_count: int) -> str:
        runtime_log = self.settings.repo_root / self.settings.logs_root / "runtime.log"
        if not runtime_log.exists():
            return "<b>Runtime log</b>\n<pre>runtime.log not found</pre>"
        lines = runtime_log.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-line_count:]) if lines else "runtime.log is empty"
        tail = tail[-3500:]
        return f"<b>Runtime log</b>\n<pre>{escape(tail)}</pre>"

    def _format_status_message(self) -> str:
        runtime_log = self.settings.repo_root / self.settings.logs_root / "runtime.log"
        last_runtime = ""
        if runtime_log.exists():
            lines = runtime_log.read_text(encoding="utf-8", errors="replace").splitlines()
            last_runtime = lines[-1] if lines else "runtime.log is empty"
        generic_counts, generic_health = self._load_generic_status()
        sports_counts, sports_health = self._load_sports_status()
        status_lines = [
            "<b>Scanner status</b>",
        ]
        if generic_counts:
            status_lines.extend(
                [
                    "<b>Generic</b>",
                    f"active_markets_now={generic_counts.get('active_markets_now', 0)}",
                    f"fresh_pairs_now={generic_counts.get('fresh_pairs_now', 0)}",
                    f"stale_pairs_now={generic_counts.get('stale_pairs_now', 0)}",
                    f"eligible_now={generic_counts.get('eligible_now', 0)}",
                ]
            )
        if sports_counts:
            status_lines.extend(
                [
                    "<b>Sports</b>",
                    f"venue_markets={sports_counts.get('venue_markets', 0)}",
                    f"canonical_events={sports_counts.get('canonical_events', 0)}",
                    f"linked_events={sports_counts.get('linked_events', 0)}",
                    f"review_items={sports_counts.get('review_items', 0)}",
                    f"eligible_now={sports_counts.get('eligible_now', 0)}",
                ]
            )
        venue_lines = self._format_health_summary_lines(generic_health, sports_health)
        if venue_lines:
            status_lines.append("<b>Venues</b>")
            status_lines.extend(venue_lines)
        status_lines.extend(
            [
            f"Last runtime line:",
            f"<pre>{escape((last_runtime or 'n/a')[-3500:])}</pre>",
            ]
        )
        return "\n".join(status_lines)

    @staticmethod
    def _format_health_message(
        exchange: str,
        status: dict[str, object],
        previous_status: str,
        mode: str,
    ) -> str:
        current_status = str(status.get("status") or "unknown").upper()
        lines = [
            f"[HEALTH][{mode.upper()}] {exchange.upper()} {current_status}",
            f"Markets: {int(status.get('market_count') or 0)}",
        ]
        variant = str(status.get("variant") or "")
        if variant:
            lines.append(f"Variant: {variant}")
        completeness = status.get("completeness")
        if completeness not in (None, ""):
            try:
                lines.append(f"Completeness: {float(completeness):.2f}")
            except (TypeError, ValueError):
                pass
        snapshot_path = str(status.get("snapshot_path") or "")
        if snapshot_path:
            lines.append(f"Snapshot: {snapshot_path}")
        if current_status == "OK" and previous_status in {"error", "empty"}:
            lines.append(f"Recovered from: {previous_status.upper()}")
        error = str(status.get("error") or "")
        if error:
            lines.append(f"Error: {error[:500]}")
        return "\n".join(lines)

    def _deliver_message(self, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        if self.send_func is not None:
            self.send_func(text)
            return
        self._send_message(text, reply_markup=reply_markup)

    def _send_message(
        self,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "chat_id": chat_id or self.settings.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        if parse_mode:
            payload["parse_mode"] = parse_mode
        response = self._api_request("sendMessage", payload)
        return dict(response.get("result") or {})

    def _delete_message(self, chat_id: str, message_id: int) -> None:
        self._api_request("deleteMessage", {"chat_id": chat_id, "message_id": str(message_id)})

    def _answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self._api_request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    @staticmethod
    def _count_lines(path: Path) -> int:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)

    @staticmethod
    def _shorten(value: str, limit: int) -> str:
        stripped = " ".join(value.split())
        if len(stripped) <= limit:
            return stripped
        return f"{stripped[: limit - 3]}..."

    @staticmethod
    def _format_money(value: float) -> str:
        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"${value / 1_000:.1f}K"
        return f"${value:.0f}"

    @staticmethod
    def _format_datetime(value: object) -> str:
        if not value or not hasattr(value, "strftime"):
            return "n/a"
        return value.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _format_outcome_mapping(mapping: dict[str, str]) -> str:
        if not mapping:
            return "n/a"
        return "; ".join(f"{left}->{right}" for left, right in mapping.items())

    def _api_request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = urlencode({key: str(value) for key, value in payload.items()}).encode("utf-8")
        request = Request(
            url=f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}",
            data=data,
            method="POST",
        )
        last_error: Exception | None = None
        for delay in self.API_RETRY_DELAYS_SECONDS:
            if delay:
                sleep(delay)
            try:
                with urlopen(request, timeout=30) as response:
                    body = response.read().decode("utf-8")
                parsed = json.loads(body)
                if not parsed.get("ok"):
                    raise RuntimeError(f"telegram_api_failed method={method} body={body}")
                return parsed
            except Exception as error:  # pragma: no cover - network/runtime fallback
                last_error = error
        raise RuntimeError(f"telegram_api_failed method={method} error={last_error}")

    def _load_generic_status(self) -> tuple[dict[str, int], dict[str, dict[str, object]]]:
        from .market_db import MarketCache

        try:
            cache = MarketCache(self.settings)
        except Exception:
            return {}, {}
        try:
            return cache.live_counts(), cache.venue_health()
        finally:
            cache.close()

    def _load_sports_status(self) -> tuple[dict[str, int], dict[str, dict[str, object]]]:
        from .sports_db import SportsCache

        try:
            cache = SportsCache(self.settings)
        except Exception:
            return {}, {}
        try:
            return cache.live_counts(), cache.venue_health()
        finally:
            cache.close()

    @staticmethod
    def _format_health_summary_lines(*health_groups: dict[str, dict[str, object]]) -> list[str]:
        merged: dict[str, dict[str, object]] = {}
        for group in health_groups:
            for exchange, payload in group.items():
                current = merged.get(exchange, {})
                if not current or str(payload.get("last_success_at") or "") >= str(current.get("last_success_at") or ""):
                    merged[exchange] = payload
        lines: list[str] = []
        for exchange in sorted(merged):
            payload = merged[exchange]
            status = str(payload.get("status") or "unknown")
            variant = str(payload.get("variant") or "default")
            completeness = payload.get("completeness")
            last_success = str(payload.get("last_success_at") or "n/a")
            completeness_text = ""
            if completeness not in (None, ""):
                try:
                    completeness_text = f" completeness={float(completeness):.2f}"
                except (TypeError, ValueError):
                    completeness_text = ""
            lines.append(
                f"{exchange}: status={status} variant={variant}{completeness_text} last_success_at={last_success}"
            )
        return lines
