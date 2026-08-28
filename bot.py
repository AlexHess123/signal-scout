#!/usr/bin/env python3
"""Signal-first trade monitor for prediction markets.

This script is built around three layers:
1. Detect: ingest raw trade events.
2. Filter: score events using wallet quality, size, price movement, and clustering.
3. Decide: emit alerts only when the signal is strong and still actionable.

It runs without external dependencies and supports:
- `simulate` mode for local testing
- `jsonl` mode for replaying or tailing newline-delimited JSON events

The goal is not to blindly copy trades. The goal is to surface high-signal
behavior early enough to decide whether it is worth following.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import ssl
import sys
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

MIN_ALERT_SCORE = 10.6
STALE_SIGNAL_SECONDS = 600
ALERT_COOLDOWN_SECONDS = 90
CLUSTER_WINDOW_SECONDS = 20
WALLET_HISTORY_SIZE = 50
MARKET_HISTORY_SIZE = 200
POLYMARKET_DATA_API = "https://data-api.polymarket.com"
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
HTTP_RETRIES = 3
HTTP_RETRY_DELAY_SECONDS = 1.5
MIN_ALERT_TRADE_SIZE_USD = 15000.0
AGGREGATION_WINDOW_SECONDS = 60.0
DEFAULT_AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "trade_audit.jsonl"))
DEFAULT_EVALUATION_LOG_PATH = Path(
    os.environ.get("EVALUATION_LOG_PATH", "trade_audit_evaluations.jsonl")
)
FOLLOW_THROUGH_AGE_SECONDS = 2 * 3600.0
FOLLOW_THROUGH_MOVE = 0.03
EVALUATION_REFRESH_SECONDS = 900.0
MAX_EVALUATIONS_PER_PASS = 200
MAX_INVALID_SLUGS_PER_PASS = 1000
AUDIT_SCHEMA_VERSION = 2


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_text(value: object, *, max_length: int = 240) -> str:
    text = str(value)
    sanitized = "".join(
        " " if char.isspace() else char for char in text if char.isprintable() or char.isspace()
    )
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > max_length:
        return f"{sanitized[: max_length - 1]}..."
    return sanitized


def looks_like_market_slug(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if " " in candidate:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    return all(char in allowed for char in candidate)


def require_https_url(value: str, *, field_name: str = "URL") -> str:
    """Return a normalized HTTPS URL or reject unsafe/malformed input."""

    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{field_name} must be an HTTPS URL without embedded credentials")
    return candidate


def audit_record_key_from_values(
    *,
    wallet: str,
    market_id: str,
    outcome: str,
    side: str,
    event_timestamp: float,
    size_usd: float,
    transaction_hash: str,
) -> str:
    if transaction_hash:
        return transaction_hash
    return f"{wallet}:{market_id}:{outcome}:{side}:{event_timestamp:.3f}:{size_usd:.2f}"


def build_ssl_context(allow_insecure_ssl: bool) -> ssl.SSLContext:
    if allow_insecure_ssl:
        # This path exists only for local simulation/JSONL troubleshooting. main()
        # rejects the option before live mode can construct this context.
        return ssl._create_unverified_context()  # nosec B323

    cafile_candidates = [
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
        "/Applications/Python 3.12/Install Certificates.command",
    ]
    for candidate in cafile_candidates:
        path = Path(candidate)
        if path.is_file() and path.suffix in {".pem", ".crt"}:
            return ssl.create_default_context(cafile=str(path))

    return ssl.create_default_context()


@dataclass(slots=True)
class TradeEvent:
    timestamp: float
    market_id: str
    market_slug: str
    market_lookup_slug: str
    outcome: str
    side: str
    price: float
    size_usd: float
    shares: float
    wallet: str
    transaction_hash: str = ""
    wallet_label: str = ""
    fill_count: int = 1

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not 0.0 <= self.price <= 1.0 or not math.isfinite(self.price):
            raise ValueError("price must be a finite value between 0 and 1")
        if self.size_usd < 0.0 or not math.isfinite(self.size_usd):
            raise ValueError("size_usd must be a finite nonnegative value")
        if self.shares < 0.0 or not math.isfinite(self.shares):
            raise ValueError("shares must be a finite nonnegative value")
        if self.timestamp < 0.0 or not math.isfinite(self.timestamp):
            raise ValueError("timestamp must be a finite nonnegative value")
        if not all((self.market_id, self.market_slug, self.outcome, self.wallet)):
            raise ValueError("market_id, market_slug, outcome, and wallet must not be empty")
        if self.fill_count < 1:
            raise ValueError("fill_count must be at least 1")

    @classmethod
    def from_dict(cls, payload: dict) -> TradeEvent:
        required = {
            "timestamp",
            "market_id",
            "market_slug",
            "outcome",
            "side",
            "price",
            "size_usd",
            "shares",
            "wallet",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(f"missing fields: {', '.join(missing)}")

        return cls(
            timestamp=float(payload["timestamp"]),
            market_id=str(payload["market_id"]),
            market_slug=str(payload["market_slug"]),
            market_lookup_slug=str(payload.get("market_lookup_slug", payload["market_slug"])),
            outcome=str(payload["outcome"]).upper(),
            side=str(payload["side"]).upper(),
            price=float(payload["price"]),
            size_usd=float(payload["size_usd"]),
            shares=float(payload["shares"]),
            wallet=str(payload["wallet"]).lower(),
            transaction_hash=str(payload.get("transaction_hash", "")),
            wallet_label=str(payload.get("wallet_label", "")),
            fill_count=int(payload.get("fill_count", 1)),
        )

    @property
    def market_key(self) -> tuple[str, str]:
        return (self.market_id, self.outcome)

    @property
    def direction(self) -> str:
        if self.side == "BUY":
            return self.outcome
        return f"SELL_{self.outcome}"

    @property
    def record_key(self) -> str:
        return audit_record_key_from_values(
            wallet=self.wallet,
            market_id=self.market_id,
            outcome=self.outcome,
            side=self.side,
            event_timestamp=self.timestamp,
            size_usd=self.size_usd,
            transaction_hash=self.transaction_hash,
        )


@dataclass(slots=True)
class WalletProfile:
    label: str
    quality: float = 0.5
    hit_rate: float = 0.5
    roi: float = 0.0
    conviction: float = 0.5
    observed_trades: int = 0

    def quality_score(self) -> float:
        base = self.quality * 5.0
        roi_bonus = clamp(self.roi / 20.0, -1.0, 2.0)
        hit_bonus = clamp((self.hit_rate - 0.5) * 4.0, -1.0, 2.0)
        conviction_bonus = clamp((self.conviction - 0.5) * 2.0, -0.5, 1.0)
        experience_bonus = clamp(self.observed_trades / 100.0, 0.0, 1.0)
        return clamp(
            base + roi_bonus + hit_bonus + conviction_bonus + experience_bonus,
            0.0,
            10.0,
        )


@dataclass(slots=True)
class SignalDecision:
    should_alert: bool
    score: float
    reasons: list[str]
    summary: str
    context: dict[str, float | int | str]

    @property
    def confidence_pct(self) -> int:
        return max(1, min(99, round(self.score / 15.0 * 100)))


@dataclass(slots=True)
class LeaderboardTrader:
    rank: int
    wallet: str
    username: str
    pnl: float
    volume: float
    verified: bool

    def profile(self) -> WalletProfile:
        pnl_score = clamp(self.pnl / 100_000.0, 0.0, 1.0)
        volume_score = clamp(self.volume / 1_000_000.0, 0.0, 1.0)
        quality = clamp(0.55 + pnl_score * 0.3 + volume_score * 0.15, 0.45, 0.97)
        hit_rate = clamp(0.52 + pnl_score * 0.18, 0.5, 0.78)
        conviction = clamp(0.6 + volume_score * 0.2, 0.55, 0.85)
        roi = clamp(self.pnl / max(self.volume, 1.0) * 100.0, -10.0, 35.0)
        label = self.username or f"rank_{self.rank}"
        if self.verified:
            label = f"{label} [verified]"
        return WalletProfile(
            label=label,
            quality=quality,
            hit_rate=hit_rate,
            roi=roi,
            conviction=conviction,
        )


@dataclass(slots=True)
class MarketState:
    prices: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=MARKET_HISTORY_SIZE)
    )
    recent_trades: deque[TradeEvent] = field(
        default_factory=lambda: deque(maxlen=MARKET_HISTORY_SIZE)
    )
    last_alert_time: float = 0.0
    last_alert_score: float = 0.0

    def push(self, event: TradeEvent) -> None:
        self.prices.append((event.timestamp, event.price))
        self.recent_trades.append(event)

    def previous_price(self) -> float | None:
        if len(self.prices) < 2:
            return None
        return self.prices[-2][1]

    def price_before(self, timestamp: float, seconds_back: float) -> float | None:
        cutoff = timestamp - seconds_back
        for ts, price in reversed(self.prices):
            if ts <= cutoff:
                return price
        return self.prices[0][1] if self.prices else None


class WalletRegistry:
    def __init__(self, profiles: dict[str, WalletProfile] | None = None) -> None:
        self._profiles: dict[str, WalletProfile] = profiles or {}
        self._recent_activity: dict[str, deque[TradeEvent]] = defaultdict(
            lambda: deque(maxlen=WALLET_HISTORY_SIZE)
        )

    def profile_for(self, wallet: str) -> WalletProfile:
        profile = self._profiles.get(wallet)
        if profile is None:
            profile = WalletProfile(label="unknown", quality=0.25, conviction=0.4)
            self._profiles[wallet] = profile
        return profile

    def record(self, event: TradeEvent) -> None:
        self._recent_activity[event.wallet].append(event)
        profile = self.profile_for(event.wallet)
        profile.observed_trades += 1

    def infer_intent(self, event: TradeEvent) -> tuple[str, float]:
        recent = self._recent_activity[event.wallet]
        same_market = [trade for trade in recent if trade.market_key == event.market_key]
        if not same_market:
            return ("fresh_entry", 1.0)

        recent_buys = [trade for trade in same_market if trade.side == "BUY"]
        recent_sells = [trade for trade in same_market if trade.side == "SELL"]

        if event.side == "BUY" and recent_buys:
            prior_size = sum(trade.size_usd for trade in recent_buys[:-1])
            if prior_size > 0 and event.size_usd >= 0.75 * prior_size:
                return ("add_to_winner", 0.9)
            return ("repeat_entry", 0.75)

        if event.side == "SELL" and recent_buys:
            buy_total = sum(trade.size_usd for trade in recent_buys)
            if buy_total > 0 and event.size_usd >= 0.5 * buy_total:
                return ("possible_exit", 0.35)
            return ("trim", 0.45)

        if event.side == "BUY" and recent_sells:
            return ("reversal_entry", 0.55)

        return ("unclear", 0.5)


class SignalEngine:
    def __init__(
        self,
        wallet_registry: WalletRegistry,
        min_alert_score: float = MIN_ALERT_SCORE,
        stale_after_seconds: int = STALE_SIGNAL_SECONDS,
        cooldown_seconds: int = ALERT_COOLDOWN_SECONDS,
        cluster_window_seconds: int = CLUSTER_WINDOW_SECONDS,
    ) -> None:
        self.wallet_registry = wallet_registry
        self.min_alert_score = min_alert_score
        self.stale_after_seconds = stale_after_seconds
        self.cooldown_seconds = cooldown_seconds
        self.cluster_window_seconds = cluster_window_seconds
        self.market_states: dict[tuple[str, str], MarketState] = defaultdict(MarketState)

    def process(self, event: TradeEvent) -> SignalDecision:
        state = self.market_states[event.market_key]
        previous_price = state.previous_price()
        lookback_price = state.price_before(event.timestamp, 60)
        wallet_profile = self.wallet_registry.profile_for(event.wallet)
        intent, intent_confidence = self.wallet_registry.infer_intent(event)

        price_move = 0.0
        if previous_price is not None and previous_price > 0:
            price_move = (event.price - previous_price) / previous_price * 100.0

        minute_move = 0.0
        if lookback_price is not None and lookback_price > 0:
            minute_move = (event.price - lookback_price) / lookback_price * 100.0

        cluster_count, cluster_wallets, cluster_size = self._cluster_metrics(state, event)
        state.push(event)
        self.wallet_registry.record(event)

        signal_score, reasons = self._score_event(
            event=event,
            wallet_profile=wallet_profile,
            price_move=price_move,
            minute_move=minute_move,
            cluster_count=cluster_count,
            cluster_wallets=cluster_wallets,
            cluster_size=cluster_size,
            intent=intent,
            intent_confidence=intent_confidence,
        )

        signal_age = time.time() - event.timestamp
        timing_ok = signal_age <= self.stale_after_seconds
        cooldown_ok = (event.timestamp - state.last_alert_time) >= self.cooldown_seconds
        elite_cluster = cluster_wallets >= 2 and cluster_size >= 5000
        override_cooldown = elite_cluster and signal_score >= (state.last_alert_score + 2.0)
        size_ok = event.size_usd >= MIN_ALERT_TRADE_SIZE_USD
        whale_single = (
            event.side == "BUY"
            and intent in {"fresh_entry", "add_to_winner", "repeat_entry"}
            and event.size_usd >= MIN_ALERT_TRADE_SIZE_USD
            and (
                wallet_profile.quality_score() >= 5.5
                or abs(minute_move) >= 4.0
                or event.price <= 0.20
                or event.price >= 0.80
            )
        )

        should_alert = (
            signal_score >= self.min_alert_score
            and size_ok
            and timing_ok
            and (elite_cluster or whale_single)
            and (cooldown_ok or override_cooldown)
        )

        if should_alert:
            state.last_alert_time = event.timestamp
            state.last_alert_score = signal_score
            reasons.append("timing still actionable")
        elif not timing_ok:
            reasons.append(f"signal stale ({signal_age:.1f}s old)")
        elif signal_score < self.min_alert_score:
            reasons.append("score below alert threshold")
        elif not size_ok:
            reasons.append(f"trade size below ${MIN_ALERT_TRADE_SIZE_USD:,.0f}")
        elif not (elite_cluster or whale_single):
            reasons.append("not whale-like enough yet")
        elif not cooldown_ok:
            reasons.append("suppressed by market cooldown")

        summary = self._build_summary(
            event=event,
            signal_score=signal_score,
            cluster_wallets=cluster_wallets,
            cluster_count=cluster_count,
            cluster_size=cluster_size,
            price_move=price_move,
            minute_move=minute_move,
            intent=intent,
            wallet_profile=wallet_profile,
        )

        return SignalDecision(
            should_alert=should_alert,
            score=round(signal_score, 2),
            reasons=reasons,
            summary=summary,
            context={
                "cluster_count": cluster_count,
                "cluster_wallets": cluster_wallets,
                "cluster_size_usd": round(cluster_size, 2),
                "fill_count": event.fill_count,
                "price_move_pct": round(price_move, 2),
                "minute_move_pct": round(minute_move, 2),
                "wallet_quality": round(wallet_profile.quality_score(), 2),
                "intent": intent,
                "signal_age_seconds": round(signal_age, 2),
            },
        )

    def _cluster_metrics(self, state: MarketState, event: TradeEvent) -> tuple[int, int, float]:
        cutoff = event.timestamp - self.cluster_window_seconds
        trades = [
            trade
            for trade in state.recent_trades
            if trade.timestamp >= cutoff and trade.side == event.side
        ]
        wallets = {trade.wallet for trade in trades}
        wallets.add(event.wallet)
        cluster_size = sum(trade.size_usd for trade in trades) + event.size_usd
        return len(trades) + 1, len(wallets), cluster_size

    def _score_event(
        self,
        *,
        event: TradeEvent,
        wallet_profile: WalletProfile,
        price_move: float,
        minute_move: float,
        cluster_count: int,
        cluster_wallets: int,
        cluster_size: float,
        intent: str,
        intent_confidence: float,
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        score = 0.0

        wallet_score = wallet_profile.quality_score() * 0.45
        score += wallet_score
        if wallet_score >= 2.0:
            reasons.append(f"strong wallet quality ({wallet_profile.label})")

        if event.size_usd >= 5000:
            score += 3.0
            reasons.append(f"large size ${event.size_usd:,.0f}")
        elif event.size_usd >= 2000:
            score += 2.0
            reasons.append(f"meaningful size ${event.size_usd:,.0f}")
        elif event.size_usd >= 750:
            score += 1.0

        edge_distance = abs(event.price - 0.5)
        if event.price <= 0.15 or event.price >= 0.85:
            score += 1.25
            reasons.append(f"high-conviction price zone {event.price:.2f}")
        elif edge_distance >= 0.2:
            score += 0.5

        if abs(price_move) >= 3.0:
            score += 2.0
            reasons.append(f"fast tape move {price_move:+.1f}%")
        elif abs(price_move) >= 1.5:
            score += 1.0

        if abs(minute_move) >= 5.0:
            score += 1.5
            reasons.append(f"1m move {minute_move:+.1f}%")
        elif abs(minute_move) >= 2.5:
            score += 0.75

        if cluster_wallets >= 3:
            score += 2.5
            reasons.append(f"{cluster_wallets} wallets clustered")
        elif cluster_wallets == 2:
            score += 1.0

        if cluster_size >= 6000:
            score += 2.0
            reasons.append(f"cluster size ${cluster_size:,.0f}")
        elif cluster_size >= 2500:
            score += 1.0

        if cluster_count >= 3:
            score += 1.0

        intent_score = {
            "fresh_entry": 1.5,
            "add_to_winner": 1.25,
            "repeat_entry": 0.8,
            "reversal_entry": 0.3,
            "trim": -0.4,
            "possible_exit": -1.25,
            "unclear": 0.0,
        }.get(intent, 0.0)
        score += intent_score * intent_confidence
        if intent in {"fresh_entry", "add_to_winner"}:
            reasons.append(f"intent looks like {intent.replace('_', ' ')}")
        elif intent in {"possible_exit", "trim"}:
            reasons.append(f"intent weakens signal ({intent.replace('_', ' ')})")

        if event.side == "SELL":
            score -= 0.75
            reasons.append("sell-side trade needs more caution")

        return score, reasons

    def _build_summary(
        self,
        *,
        event: TradeEvent,
        signal_score: float,
        cluster_wallets: int,
        cluster_count: int,
        cluster_size: float,
        price_move: float,
        minute_move: float,
        intent: str,
        wallet_profile: WalletProfile,
    ) -> str:
        implied_pct = round(event.price * 100)
        confidence_pct = max(1, min(99, round(signal_score / 15.0 * 100)))
        trader_name = safe_text(
            event.wallet_label or wallet_profile.label or event.wallet, max_length=80
        )
        action_word = "bought" if event.side == "BUY" else "sold"
        fill_line = f"\nFills: {event.fill_count}" if event.fill_count > 1 else ""
        return (
            f"{trader_name} {action_word} {safe_text(event.outcome, max_length=40)} at {implied_pct}%\n"
            f"Bot likes it: {confidence_pct}%\n"
            f"Size: ${event.size_usd:,.0f}\n"
            f"Market: {safe_text(event.market_slug, max_length=160)}{fill_line}"
        )


def notify_stdout(decision: SignalDecision) -> None:
    print("\n=== ALERT ===")
    print(decision.summary)
    print("Reasons:", "; ".join(decision.reasons))
    print("Context:", json.dumps(decision.context, sort_keys=True))
    print("=============\n")


class NotificationSink:
    def send(self, decision: SignalDecision) -> None:
        raise NotImplementedError


class StdoutNotifier(NotificationSink):
    def send(self, decision: SignalDecision) -> None:
        notify_stdout(decision)


class NtfyNotifier(NotificationSink):
    def __init__(
        self, topic: str, ssl_context: ssl.SSLContext, base_url: str = "https://ntfy.sh"
    ) -> None:
        normalized_base_url = require_https_url(base_url, field_name="ntfy URL")
        normalized_topic = topic.strip()
        if not normalized_topic:
            raise ValueError("NTFY_TOPIC must not be empty")
        self.url = f"{normalized_base_url.rstrip('/')}/{quote(normalized_topic, safe='')}"
        self.ssl_context = ssl_context

    def send(self, decision: SignalDecision) -> None:
        body = f"{decision.summary}\nReasons: {'; '.join(decision.reasons)}"
        request = Request(
            self.url,
            data=body.encode("utf-8"),
            headers={
                "Title": "Polymarket signal",
                "Priority": "urgent" if decision.score >= 10 else "high",
                "Tags": "chart_with_upwards_trend,moneybag",
            },
            method="POST",
        )
        # self.url is validated as HTTPS in __init__.
        with urlopen(  # nosec B310
            request, timeout=15, context=self.ssl_context
        ):
            pass


class PushoverNotifier(NotificationSink):
    def __init__(self, app_token: str, user_key: str, ssl_context: ssl.SSLContext) -> None:
        self.app_token = app_token
        self.user_key = user_key
        self.ssl_context = ssl_context

    def send(self, decision: SignalDecision) -> None:
        payload = urlencode(
            {
                "token": self.app_token,
                "user": self.user_key,
                "title": "Polymarket signal",
                "message": f"{decision.summary}\nReasons: {'; '.join(decision.reasons)}",
                "priority": 1 if decision.score >= 10 else 0,
            }
        ).encode("utf-8")
        request = Request(
            "https://api.pushover.net/1/messages.json",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        # The destination is a fixed HTTPS Pushover endpoint.
        with urlopen(  # nosec B310
            request, timeout=15, context=self.ssl_context
        ):
            pass


def send_startup_notification(notifier: NotificationSink, mode: str) -> None:
    if isinstance(notifier, StdoutNotifier) or mode != "live":
        return

    started_at = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    decision = SignalDecision(
        should_alert=True,
        score=9.0,
        reasons=["worker deployed successfully", "live polling started"],
        summary=(
            "Polymarket bot is running\n"
            "Bot likes it: 100%\n"
            "Size: startup check\n"
            f"Market: Live worker started at {started_at}"
        ),
        context={"startup": "true", "mode": mode},
    )
    notifier.send(decision)


def append_audit_record(
    path: Path,
    *,
    event: TradeEvent,
    decision: SignalDecision,
    status: str,
) -> None:
    record = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "record_key": event.record_key,
        "recorded_at": time.time(),
        "status": status,
        "wallet": event.wallet,
        "wallet_label": safe_text(event.wallet_label, max_length=120),
        "market_id": event.market_id,
        "market_slug": safe_text(event.market_slug, max_length=200),
        "market_lookup_slug": event.market_lookup_slug,
        "outcome": safe_text(event.outcome, max_length=40),
        "side": event.side,
        "price": event.price,
        "size_usd": event.size_usd,
        "shares": event.shares,
        "fill_count": event.fill_count,
        "transaction_hash": event.transaction_hash,
        "event_timestamp": event.timestamp,
        "score": decision.score,
        "confidence_pct": decision.confidence_pct,
        "reasons": decision.reasons,
        "summary": decision.summary,
        "context": decision.context,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load_wallet_profiles(path: Path | None) -> dict[str, WalletProfile]:
    if path is None or not path.exists():
        return {
            "0xalpha": WalletProfile(
                "elite", quality=0.92, hit_rate=0.68, roi=24.0, conviction=0.84
            ),
            "0xbeta": WalletProfile(
                "sharp", quality=0.84, hit_rate=0.63, roi=16.0, conviction=0.76
            ),
            "0xgamma": WalletProfile(
                "solid", quality=0.72, hit_rate=0.58, roi=9.5, conviction=0.66
            ),
            "0xdelta": WalletProfile(
                "watchlist", quality=0.60, hit_rate=0.54, roi=4.0, conviction=0.52
            ),
        }

    raw = json.loads(path.read_text(encoding="utf-8"))
    profiles: dict[str, WalletProfile] = {}
    for wallet, payload in raw.items():
        profiles[wallet.lower()] = WalletProfile(
            label=str(payload.get("label", "tracked")),
            quality=float(payload.get("quality", 0.5)),
            hit_rate=float(payload.get("hit_rate", 0.5)),
            roi=float(payload.get("roi", 0.0)),
            conviction=float(payload.get("conviction", 0.5)),
            observed_trades=int(payload.get("observed_trades", 0)),
        )
    return profiles


def http_json(
    url: str,
    params: dict[str, str | int | float] | None = None,
    *,
    ssl_context: ssl.SSLContext,
) -> object:
    require_https_url(url)
    query = urlencode(params or {})
    full_url = url if not query else f"{url}?{query}"
    last_error: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            request = Request(full_url, headers={"User-Agent": "polymarket-signal-bot/1.0"})
            # require_https_url rejects non-HTTPS URLs.
            with urlopen(  # nosec B310
                request, timeout=20, context=ssl_context
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == HTTP_RETRIES - 1:
                break
            time.sleep(HTTP_RETRY_DELAY_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error


def parse_jsonish_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return parsed
    return []


def outcome_price_for_market(market: dict, outcome: str) -> float | None:
    outcomes = [str(item).upper() for item in parse_jsonish_list(market.get("outcomes"))]
    prices = parse_jsonish_list(market.get("outcomePrices"))
    if not outcomes or len(outcomes) != len(prices):
        return None
    try:
        index = outcomes.index(str(outcome).upper())
    except ValueError:
        return None
    try:
        return float(prices[index])
    except (TypeError, ValueError):
        return None


def resolved_outcome_for_market(market: dict) -> str | None:
    if not bool(market.get("closed")):
        return None
    outcomes = parse_jsonish_list(market.get("outcomes"))
    prices = parse_jsonish_list(market.get("outcomePrices"))
    if not outcomes or len(outcomes) != len(prices):
        return None
    numeric_prices: list[float] = []
    for value in prices:
        try:
            numeric_prices.append(float(value))
        except (TypeError, ValueError):
            return None
    winning = [index for index, price in enumerate(numeric_prices) if price >= 0.99]
    losing = [index for index, price in enumerate(numeric_prices) if price <= 0.01]
    if len(winning) != 1 or len(losing) != len(numeric_prices) - 1:
        return None
    return str(outcomes[winning[0]]).upper()


def fetch_gamma_market_by_slug(slug: str, *, ssl_context: ssl.SSLContext) -> dict:
    payload = http_json(
        f"{POLYMARKET_GAMMA_API}/markets/slug/{quote(slug, safe='')}",
        ssl_context=ssl_context,
    )
    if not isinstance(payload, dict):
        raise TypeError(f"unexpected gamma market payload for slug {slug}")
    return payload


class AuditEvaluator:
    def __init__(
        self,
        *,
        audit_log_path: Path,
        evaluation_log_path: Path,
        ssl_context: ssl.SSLContext,
        refresh_seconds: float,
    ) -> None:
        self.audit_log_path = audit_log_path
        self.evaluation_log_path = evaluation_log_path
        self.ssl_context = ssl_context
        self.refresh_seconds = refresh_seconds
        self.last_refresh = 0.0
        self.follow_keys: set[str] = set()
        self.resolved_keys: set[str] = set()
        self.invalid_keys: set[str] = set()
        self._load_existing_evaluations()

    def _load_existing_evaluations(self) -> None:
        if not self.evaluation_log_path.exists():
            return
        with self.evaluation_log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = str(record.get("record_key", ""))
                kind = str(record.get("evaluation_kind", ""))
                if not key:
                    continue
                if kind == "follow_through":
                    self.follow_keys.add(key)
                elif kind == "resolved":
                    self.resolved_keys.add(key)
                elif kind == "invalid_slug":
                    self.invalid_keys.add(key)

    def maybe_refresh(self, now: float) -> None:
        if now - self.last_refresh < self.refresh_seconds:
            return
        self.last_refresh = now
        try:
            self.refresh(now)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            print(f"audit evaluation refresh failed: {exc}", file=sys.stderr)

    def refresh(self, now: float) -> None:
        if not self.audit_log_path.exists():
            return

        candidates: list[dict] = []
        with self.audit_log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("status") != "alert":
                    continue
                record_key = str(
                    record.get("record_key")
                    or audit_record_key_from_values(
                        wallet=str(record.get("wallet", "")),
                        market_id=str(record.get("market_id", "")),
                        outcome=str(record.get("outcome", "")),
                        side=str(record.get("side", "")),
                        event_timestamp=float(record.get("event_timestamp", 0.0) or 0.0),
                        size_usd=float(record.get("size_usd", 0.0) or 0.0),
                        transaction_hash=str(record.get("transaction_hash", "")),
                    )
                )
                recorded_at = float(record.get("recorded_at", 0.0) or 0.0)
                if now - recorded_at < FOLLOW_THROUGH_AGE_SECONDS:
                    continue
                needs_follow = record_key not in self.follow_keys
                needs_resolved = record_key not in self.resolved_keys
                if record_key in self.invalid_keys:
                    continue
                if not needs_follow and not needs_resolved:
                    continue
                record["record_key"] = record_key
                candidates.append(record)

        candidates.sort(
            key=lambda item: (
                0
                if looks_like_market_slug(
                    str(item.get("market_lookup_slug") or item.get("market_slug", ""))
                )
                else 1,
                float(item.get("recorded_at", 0.0)),
            )
        )
        if not candidates:
            return

        self.evaluation_log_path.parent.mkdir(parents=True, exist_ok=True)
        processed = 0
        invalid_processed = 0
        with self.evaluation_log_path.open("a", encoding="utf-8") as handle:
            for record in candidates:
                if processed >= MAX_EVALUATIONS_PER_PASS:
                    break
                lookup_slug = str(record.get("market_lookup_slug") or record.get("market_slug", ""))
                if not lookup_slug:
                    continue
                if not looks_like_market_slug(lookup_slug):
                    if invalid_processed >= MAX_INVALID_SLUGS_PER_PASS:
                        continue
                    payload = {
                        "record_key": str(record["record_key"]),
                        "evaluation_kind": "invalid_slug",
                        "evaluated_at": now,
                        "market_lookup_slug": lookup_slug,
                    }
                    handle.write(json.dumps(payload, sort_keys=True) + "\n")
                    self.invalid_keys.add(str(record["record_key"]))
                    invalid_processed += 1
                    continue
                try:
                    market = fetch_gamma_market_by_slug(lookup_slug, ssl_context=self.ssl_context)
                except (HTTPError, URLError, TimeoutError, TypeError, ValueError) as exc:
                    print(
                        f"audit evaluation skipped for slug {lookup_slug}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                record_key = str(record["record_key"])
                wrote = False

                if record_key not in self.follow_keys:
                    current_price = outcome_price_for_market(market, str(record.get("outcome", "")))
                    if current_price is not None:
                        entry_price = float(record.get("price", 0.0) or 0.0)
                        move = current_price - entry_price
                        if move >= FOLLOW_THROUGH_MOVE:
                            grade = "right"
                        elif move <= -FOLLOW_THROUGH_MOVE:
                            grade = "wrong"
                        else:
                            grade = "flat"
                        payload = {
                            "record_key": record_key,
                            "evaluation_kind": "follow_through",
                            "evaluated_at": now,
                            "grade": grade,
                            "current_price": current_price,
                            "move_pct": move,
                            "market_closed": bool(market.get("closed")),
                            "market_lookup_slug": lookup_slug,
                        }
                        handle.write(json.dumps(payload, sort_keys=True) + "\n")
                        self.follow_keys.add(record_key)
                        wrote = True

                if record_key not in self.resolved_keys:
                    winning_outcome = resolved_outcome_for_market(market)
                    if winning_outcome is not None:
                        trade_outcome = str(record.get("outcome", "")).upper()
                        trade_side = str(record.get("side", "")).upper()
                        if trade_side == "BUY":
                            grade = "right" if trade_outcome == winning_outcome else "wrong"
                        elif trade_side == "SELL":
                            grade = "right" if trade_outcome != winning_outcome else "wrong"
                        else:
                            grade = "unresolved"
                        payload = {
                            "record_key": record_key,
                            "evaluation_kind": "resolved",
                            "evaluated_at": now,
                            "grade": grade,
                            "winning_outcome": winning_outcome,
                            "market_closed": True,
                            "market_lookup_slug": lookup_slug,
                        }
                        handle.write(json.dumps(payload, sort_keys=True) + "\n")
                        self.resolved_keys.add(record_key)
                        wrote = True

                if wrote:
                    processed += 1


def fetch_leaderboard_traders(
    *,
    category: str,
    time_period: str,
    order_by: str,
    limit: int,
    ssl_context: ssl.SSLContext,
) -> list[LeaderboardTrader]:
    payload = http_json(
        f"{POLYMARKET_DATA_API}/v1/leaderboard",
        {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
        },
        ssl_context=ssl_context,
    )
    traders: list[LeaderboardTrader] = []
    for item in payload if isinstance(payload, list) else []:
        wallet = str(item.get("proxyWallet", "")).lower()
        if not wallet:
            continue
        rank_raw = item.get("rank", len(traders) + 1)
        try:
            rank = int(rank_raw)
        except (TypeError, ValueError):
            rank = len(traders) + 1
        traders.append(
            LeaderboardTrader(
                rank=rank,
                wallet=wallet,
                username=str(item.get("userName", "")),
                pnl=float(item.get("pnl", 0.0) or 0.0),
                volume=float(item.get("vol", 0.0) or 0.0),
                verified=bool(item.get("verifiedBadge", False)),
            )
        )
    return traders


def fetch_user_trades(
    wallet: str,
    limit: int,
    *,
    ssl_context: ssl.SSLContext,
    taker_only: bool,
) -> list[TradeEvent]:
    payload = http_json(
        f"{POLYMARKET_DATA_API}/trades",
        {
            "user": wallet,
            "limit": limit,
            "takerOnly": "true" if taker_only else "false",
        },
        ssl_context=ssl_context,
    )
    trades: list[TradeEvent] = []
    for item in payload if isinstance(payload, list) else []:
        transaction_hash = str(item.get("transactionHash", ""))
        condition_id = str(item.get("conditionId", ""))
        title = safe_text(item.get("title", "") or item.get("slug", ""), max_length=200)
        market_slug = str(item.get("slug", "") or item.get("eventSlug", "") or title)
        raw_price = float(item.get("price", 0.0) or 0.0)
        price = raw_price / 100.0 if raw_price > 1 else raw_price
        size = float(item.get("size", 0.0) or 0.0)
        if not condition_id or not title or price <= 0 or size <= 0:
            continue

        trades.append(
            TradeEvent(
                timestamp=float(item.get("timestamp", 0.0) or 0.0),
                market_id=condition_id,
                market_slug=title,
                market_lookup_slug=market_slug,
                outcome=safe_text(str(item.get("outcome", "")).upper(), max_length=40),
                side=str(item.get("side", "")).upper(),
                price=price,
                size_usd=size,
                shares=size / max(price, 0.01),
                wallet=wallet,
                transaction_hash=transaction_hash,
                wallet_label=safe_text(
                    item.get("name", "") or item.get("pseudonym", ""), max_length=120
                ),
            )
        )
    trades.sort(key=lambda trade: trade.timestamp)
    return trades


def iter_jsonl_events(path: Path | None, follow: bool = False) -> Iterator[TradeEvent]:
    handle = sys.stdin if path is None else path.open("r", encoding="utf-8")

    try:
        while True:
            line = handle.readline()
            if not line:
                if follow:
                    time.sleep(0.25)
                    continue
                break

            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            try:
                yield TradeEvent.from_dict(json.loads(stripped))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                print(f"Skipping invalid event: {exc}", file=sys.stderr)
    finally:
        if path is not None:
            handle.close()


def simulated_events() -> Iterator[TradeEvent]:
    event_ts = time.time() - 18.0
    wallets = ["0xalpha", "0xbeta", "0xgamma", "0xdelta", "0xnoise1", "0xnoise2"]
    markets = [
        ("election-2028", "Will candidate A win?", "YES"),
        ("fed-june", "Will the Fed cut in June?", "YES"),
        ("btc-100k", "Will BTC hit 100k this year?", "YES"),
    ]

    prices = {
        ("election-2028", "YES"): 0.42,
        ("fed-june", "YES"): 0.18,
        ("btc-100k", "YES"): 0.63,
    }

    scripted = [
        ("0xalpha", "fed-june", "Will the Fed cut in June?", "YES", "BUY", 2400, 0.18),
        ("0xbeta", "fed-june", "Will the Fed cut in June?", "YES", "BUY", 3100, 0.19),
        ("0xgamma", "fed-june", "Will the Fed cut in June?", "YES", "BUY", 2200, 0.21),
        (
            "0xnoise1",
            "btc-100k",
            "Will BTC hit 100k this year?",
            "YES",
            "BUY",
            160,
            0.64,
        ),
        (
            "0xnoise2",
            "btc-100k",
            "Will BTC hit 100k this year?",
            "YES",
            "SELL",
            210,
            0.63,
        ),
        ("0xdelta", "election-2028", "Will candidate A win?", "YES", "BUY", 900, 0.43),
        ("0xalpha", "fed-june", "Will the Fed cut in June?", "YES", "SELL", 4200, 0.28),
    ]

    for wallet, market_id, slug, outcome, side, size_usd, next_price in scripted:
        event_ts += 6.0
        old_price = prices[(market_id, outcome)]
        prices[(market_id, outcome)] = next_price
        yield TradeEvent(
            timestamp=event_ts,
            market_id=market_id,
            market_slug=slug,
            market_lookup_slug=market_id,
            outcome=outcome,
            side=side,
            price=next_price,
            size_usd=float(size_usd),
            shares=size_usd / max(next_price, 0.01),
            wallet=wallet,
        )
        _ = old_price

    while True:
        market_id, slug, outcome = random.choice(markets)
        wallet = random.choice(wallets)
        side = "BUY" if random.random() > 0.25 else "SELL"
        current_price = prices[(market_id, outcome)]
        step = random.uniform(-0.03, 0.03)
        next_price = clamp(current_price + step, 0.03, 0.97)
        prices[(market_id, outcome)] = next_price

        tracked_multiplier = 1.0
        if wallet in {"0xalpha", "0xbeta", "0xgamma"} and random.random() > 0.6:
            tracked_multiplier = random.uniform(3.0, 7.0)

        size_usd = random.uniform(150, 1400) * tracked_multiplier
        event_ts = min(time.time() - 0.25, event_ts + random.uniform(2.0, 8.0))
        yield TradeEvent(
            timestamp=event_ts,
            market_id=market_id,
            market_slug=slug,
            market_lookup_slug=market_id,
            outcome=outcome,
            side=side,
            price=next_price,
            size_usd=size_usd,
            shares=size_usd / max(next_price, 0.01),
            wallet=wallet,
        )


def iter_live_trader_events(
    *,
    category: str,
    time_period: str,
    order_by: str,
    leaderboard_limit: int,
    trades_per_wallet: int,
    poll_seconds: float,
    leaderboard_refresh_seconds: float,
    wallet_registry: WalletRegistry,
    ssl_context: ssl.SSLContext,
    taker_only: bool,
) -> Iterator[TradeEvent]:
    seen_trade_ids: deque[str] = deque()
    seen_lookup: set[str] = set()
    tracked_wallets: list[str] = []
    next_leaderboard_refresh = 0.0

    while True:
        now = time.time()
        if now >= next_leaderboard_refresh or not tracked_wallets:
            try:
                traders = fetch_leaderboard_traders(
                    category=category,
                    time_period=time_period,
                    order_by=order_by,
                    limit=leaderboard_limit,
                    ssl_context=ssl_context,
                )
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"leaderboard fetch failed: {exc}", file=sys.stderr)
                time.sleep(poll_seconds)
                continue
            tracked_wallets = [trader.wallet for trader in traders]
            for trader in traders:
                wallet_registry._profiles[trader.wallet] = trader.profile()
            next_leaderboard_refresh = now + leaderboard_refresh_seconds
            print(
                f"tracking {len(tracked_wallets)} leaderboard wallets "
                f"({category}/{time_period}/{order_by})",
                file=sys.stderr,
            )

        for wallet in tracked_wallets:
            try:
                trades = fetch_user_trades(
                    wallet,
                    trades_per_wallet,
                    ssl_context=ssl_context,
                    taker_only=taker_only,
                )
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"trade fetch failed for {wallet}: {exc}", file=sys.stderr)
                continue

            for trade in trades:
                dedupe_key = trade.transaction_hash or (
                    f"{trade.wallet}:{trade.market_id}:{trade.side}:{trade.timestamp}:{trade.size_usd:.2f}"
                )
                if dedupe_key in seen_lookup:
                    continue

                seen_lookup.add(dedupe_key)
                seen_trade_ids.append(dedupe_key)
                while len(seen_trade_ids) > 20_000:
                    stale = seen_trade_ids.popleft()
                    seen_lookup.discard(stale)

                yield trade

        time.sleep(poll_seconds)


class AggregatedEvents:
    def __init__(self, events: Iterable[TradeEvent], window_seconds: float) -> None:
        self.events = iter(events)
        self.window_seconds = window_seconds
        self.pending: dict[tuple[str, str, str, str], TradeEvent] = {}

    def __iter__(self) -> Iterator[TradeEvent]:
        for event in self.events:
            yield from self._flush_expired(event.timestamp)

            key = (event.wallet, event.market_id, event.outcome, event.side)
            current = self.pending.get(key)
            if current is None:
                self.pending[key] = event
                continue

            if event.timestamp - current.timestamp <= self.window_seconds:
                self.pending[key] = self._merge(current, event)
            else:
                yield current
                self.pending[key] = event

        yield from sorted(self.pending.values(), key=lambda trade: trade.timestamp)

    def _flush_expired(self, current_timestamp: float) -> Iterator[TradeEvent]:
        expired_keys = [
            key
            for key, trade in self.pending.items()
            if current_timestamp - trade.timestamp > self.window_seconds
        ]
        for key in sorted(expired_keys, key=lambda item: self.pending[item].timestamp):
            yield self.pending.pop(key)

    @staticmethod
    def _merge(existing: TradeEvent, incoming: TradeEvent) -> TradeEvent:
        total_size = existing.size_usd + incoming.size_usd
        weighted_price = (
            (existing.price * existing.size_usd) + (incoming.price * incoming.size_usd)
        ) / max(total_size, 0.01)
        return TradeEvent(
            timestamp=max(existing.timestamp, incoming.timestamp),
            market_id=existing.market_id,
            market_slug=existing.market_slug,
            market_lookup_slug=existing.market_lookup_slug,
            outcome=existing.outcome,
            side=existing.side,
            price=weighted_price,
            size_usd=total_size,
            shares=existing.shares + incoming.shares,
            wallet=existing.wallet,
            transaction_hash=incoming.transaction_hash or existing.transaction_hash,
            wallet_label=existing.wallet_label or incoming.wallet_label,
            fill_count=existing.fill_count + incoming.fill_count,
        )


def run(
    engine: SignalEngine,
    events: Iterable[TradeEvent],
    print_all: bool,
    notifier: NotificationSink,
    audit_log_path: Path,
    evaluator: AuditEvaluator | None,
) -> None:
    for event in events:
        if evaluator is not None:
            evaluator.maybe_refresh(time.time())
        print(
            "seen | "
            f"wallet={safe_text(event.wallet_label or event.wallet, max_length=80)} | "
            f"side={safe_text(event.side, max_length=12)} {safe_text(event.outcome, max_length=40)} | "
            f"price={event.price:.2f} | "
            f"size=${event.size_usd:,.0f} | "
            f"fills={event.fill_count} | "
            f"market={safe_text(event.market_slug, max_length=160)}",
            file=sys.stderr,
        )
        decision = engine.process(event)
        if decision.should_alert:
            print(f"alert | {decision.summary}", file=sys.stderr)
            append_audit_record(audit_log_path, event=event, decision=decision, status="alert")
            notifier.send(decision)
        else:
            joined_reasons = "; ".join(decision.reasons)
            print(
                f"ignored | {decision.summary} | reasons={joined_reasons}",
                file=sys.stderr,
            )
            append_audit_record(audit_log_path, event=event, decision=decision, status="ignored")
        if print_all and not decision.should_alert:
            print(f"pass | {decision.summary}")
            print("reasons:", "; ".join(decision.reasons))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prediction market signal monitor")
    parser.add_argument(
        "--mode",
        choices=("simulate", "jsonl", "live"),
        default="simulate",
        help="simulate generates fake trades; jsonl reads newline-delimited JSON; live tracks leaderboard traders",
    )
    parser.add_argument(
        "--events-file",
        type=Path,
        help="JSONL file to read when using --mode jsonl; omit to read stdin",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="tail the JSONL file or stdin stream instead of exiting at EOF",
    )
    parser.add_argument(
        "--wallets-file",
        type=Path,
        help="JSON file with tracked wallet profiles",
    )
    parser.add_argument(
        "--min-alert-score",
        type=float,
        default=MIN_ALERT_SCORE,
        help="minimum score required to alert",
    )
    parser.add_argument(
        "--print-all",
        action="store_true",
        help="print scored non-alerts for debugging",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=0,
        help="stop after N events; 0 means no limit",
    )
    parser.add_argument(
        "--leaderboard-category",
        default="OVERALL",
        help="Polymarket leaderboard category",
    )
    parser.add_argument(
        "--leaderboard-time-period", default="MONTH", help="DAY, WEEK, MONTH, or ALL"
    )
    parser.add_argument("--leaderboard-order-by", default="PNL", help="PNL or VOL")
    parser.add_argument(
        "--leaderboard-limit",
        type=int,
        default=200,
        help="top N leaderboard traders to track",
    )
    parser.add_argument(
        "--trades-per-wallet",
        type=int,
        default=25,
        help="recent trades fetched per wallet poll",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=15.0,
        help="seconds between live polling cycles",
    )
    parser.add_argument(
        "--taker-only",
        action="store_true",
        help="only fetch taker trades; disabled by default so maker fills are included",
    )
    parser.add_argument(
        "--leaderboard-refresh-seconds",
        type=float,
        default=1800.0,
        help="seconds between leaderboard refreshes",
    )
    parser.add_argument(
        "--notify-via",
        choices=("stdout", "ntfy", "pushover"),
        default="pushover",
        help="notification delivery method",
    )
    parser.add_argument("--ntfy-url", default="https://ntfy.sh", help="base URL for ntfy")
    parser.add_argument(
        "--audit-log-path",
        type=Path,
        default=DEFAULT_AUDIT_LOG_PATH,
        help="path to the JSONL audit log; use a persistent volume path in production",
    )
    parser.add_argument(
        "--evaluation-log-path",
        type=Path,
        default=DEFAULT_EVALUATION_LOG_PATH,
        help="path to the JSONL evaluation sidecar for resolved/follow-through grading",
    )
    parser.add_argument(
        "--allow-insecure-ssl",
        action="store_true",
        help="disable SSL certificate verification for broken local Python trust stores",
    )
    return parser


def build_notifier(args: argparse.Namespace, ssl_context: ssl.SSLContext) -> NotificationSink:
    if args.notify_via == "ntfy":
        topic = os.environ.get("NTFY_TOPIC")
        if not topic:
            raise ValueError("NTFY_TOPIC is required when --notify-via ntfy")
        return NtfyNotifier(topic=topic, ssl_context=ssl_context, base_url=args.ntfy_url)
    if args.notify_via == "pushover":
        app_token = os.environ.get("PUSHOVER_APP_TOKEN")
        user_key = os.environ.get("PUSHOVER_USER_KEY")
        if not app_token or not user_key:
            raise ValueError("PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY are required for Pushover")
        return PushoverNotifier(app_token, user_key, ssl_context=ssl_context)
    return StdoutNotifier()


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "live" and args.allow_insecure_ssl:
        print("--allow-insecure-ssl is not permitted in live mode", file=sys.stderr)
        return 2
    ssl_context = build_ssl_context(args.allow_insecure_ssl)

    profiles = load_wallet_profiles(args.wallets_file)
    registry = WalletRegistry(profiles)
    engine = SignalEngine(wallet_registry=registry, min_alert_score=args.min_alert_score)
    notifier = build_notifier(args, ssl_context)
    evaluator = (
        AuditEvaluator(
            audit_log_path=args.audit_log_path,
            evaluation_log_path=args.evaluation_log_path,
            ssl_context=ssl_context,
            refresh_seconds=EVALUATION_REFRESH_SECONDS,
        )
        if args.mode == "live"
        else None
    )

    if args.mode == "simulate":
        events = simulated_events()
    elif args.mode == "jsonl":
        events = iter_jsonl_events(args.events_file, follow=args.follow)
    else:
        events = iter_live_trader_events(
            category=args.leaderboard_category.upper(),
            time_period=args.leaderboard_time_period.upper(),
            order_by=args.leaderboard_order_by.upper(),
            leaderboard_limit=args.leaderboard_limit,
            trades_per_wallet=args.trades_per_wallet,
            poll_seconds=args.poll_seconds,
            leaderboard_refresh_seconds=args.leaderboard_refresh_seconds,
            wallet_registry=registry,
            ssl_context=ssl_context,
            taker_only=args.taker_only,
        )
        events = AggregatedEvents(events, AGGREGATION_WINDOW_SECONDS)

    if args.max_events > 0:
        events = LimitedEvents(events, args.max_events)

    try:
        send_startup_notification(notifier, args.mode)
        run(
            engine=engine,
            events=events,
            print_all=args.print_all,
            notifier=notifier,
            audit_log_path=args.audit_log_path,
            evaluator=evaluator,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopping.")
        return 130
    return 0


class LimitedEvents:
    def __init__(self, events: Iterable[TradeEvent], limit: int) -> None:
        self.events = iter(events)
        self.limit = limit

    def __iter__(self) -> Iterator[TradeEvent]:
        count = 0
        while count < self.limit:
            try:
                yield next(self.events)
            except StopIteration:
                return
            count += 1


if __name__ == "__main__":
    raise SystemExit(main())
