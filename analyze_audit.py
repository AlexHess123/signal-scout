#!/usr/bin/env python3
"""Build a simple HTML report from the trade audit log."""

from __future__ import annotations

import json
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from html import escape
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

DEFAULT_INPUT = Path("trade_audit.jsonl")
DEFAULT_OUTPUT = Path("trade_audit_report.html")
DEFAULT_CACHE = Path("trade_audit_market_cache.json")
DEFAULT_EVALUATIONS = Path("trade_audit_evaluations.jsonl")
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
FOLLOW_THROUGH_HOURS = 2
FOLLOW_THROUGH_MOVE = 0.03
GRADE_ORDER = ("right", "wrong", "flat", "unresolved")
GRADE_COLORS = {
    "right": "#15803d",
    "wrong": "#b91c1c",
    "flat": "#475569",
    "unresolved": "#a16207",
}


def load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def save_cache(path: Path, payload: dict[str, dict]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def load_evaluations(path: Path) -> dict[str, dict[str, dict]]:
    if not path.exists():
        return {"follow_through": {}, "resolved": {}}
    loaded: dict[str, dict[str, dict]] = {"follow_through": {}, "resolved": {}}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(record.get("evaluation_kind", ""))
            key = str(record.get("record_key", ""))
            if kind in loaded and key:
                loaded[kind][key] = record
    return loaded


def record_key(record: dict) -> str:
    transaction_hash = str(record.get("transaction_hash", ""))
    if transaction_hash:
        return transaction_hash
    return (
        f"{record.get('wallet', '')}:{record.get('market_id', '')}:{record.get('outcome', '')}:"
        f"{record.get('side', '')}:{float(record.get('event_timestamp', 0.0) or 0.0):.3f}:"
        f"{float(record.get('size_usd', 0.0) or 0.0):.2f}"
    )


def require_https_url(value: str) -> str:
    """Return a normalized HTTPS URL or reject unsafe/malformed input."""

    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL must use HTTPS without embedded credentials")
    return candidate


def http_json(url: str, params: dict | None = None) -> object:
    require_https_url(url)
    query = urlencode(params or {})
    full_url = url if not query else f"{url}?{query}"
    request = Request(full_url, headers={"User-Agent": "trade-audit-report/1.0"})
    try:
        # require_https_url rejects non-HTTPS URLs.
        with urlopen(  # nosec B310
            request, timeout=30
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError:
        # `curl` works more reliably than urllib on this host when local Python DNS/SSL
        # resolution is unstable. Fall back so report generation can still grade markets.
        try:
            result = subprocess.run(
                [
                    "curl",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--connect-timeout",
                    "10",
                    "--max-time",
                    "30",
                    "-fsSL",
                    full_url,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=35,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise URLError("HTTPS request failed through urllib and curl") from exc
        return json.loads(result.stdout)


def load_active_market_map() -> dict[str, dict]:
    markets_by_condition: dict[str, dict] = {}
    offset = 0
    limit = 100

    while True:
        try:
            payload = http_json(
                f"{POLYMARKET_GAMMA_API}/events",
                {
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
            )
        except URLError:
            return {}
        if not isinstance(payload, list) or not payload:
            break

        for event in payload:
            for market in event.get("markets", []) or []:
                condition_id = str(market.get("conditionId", ""))
                if condition_id:
                    markets_by_condition[condition_id] = market

        if len(payload) < limit:
            break
        offset += limit

    return markets_by_condition


def parse_jsonish(value: object) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            return []
    return []


def current_outcome_price(market: dict, outcome: str) -> float | None:
    outcomes = parse_jsonish(market.get("outcomes"))
    prices = parse_jsonish(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None

    normalized = [str(item).upper() for item in outcomes]
    try:
        index = normalized.index(str(outcome).upper())
    except ValueError:
        return None

    try:
        return float(prices[index])
    except (TypeError, ValueError):
        return None


def infer_resolved_outcome(market: dict) -> str | None:
    if not bool(market.get("closed")):
        return None

    outcomes = parse_jsonish(market.get("outcomes"))
    prices = parse_jsonish(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None

    numeric_prices: list[float] = []
    for value in prices:
        try:
            numeric_prices.append(float(value))
        except (TypeError, ValueError):
            return None

    winning_indexes = [index for index, price in enumerate(numeric_prices) if price >= 0.99]
    losing_indexes = [index for index, price in enumerate(numeric_prices) if price <= 0.01]
    if len(winning_indexes) != 1 or len(losing_indexes) != len(numeric_prices) - 1:
        return None

    return str(outcomes[winning_indexes[0]]).upper()


def load_closed_market_resolution_map_by_slug(
    slugs: set[str],
) -> tuple[dict[str, dict], str | None]:
    if not slugs:
        return {}, None

    cache = load_cache(DEFAULT_CACHE)
    cached_matches = {key: value for key, value in cache.items() if key in slugs}
    missing = slugs - set(cached_matches)
    if not missing:
        return cached_matches, None

    resolved: dict[str, dict] = dict(cached_matches)
    try:
        for slug in sorted(missing):
            market = http_json(f"{POLYMARKET_GAMMA_API}/markets/slug/{quote(slug, safe='')}")
            if not isinstance(market, dict):
                continue
            winning_outcome = infer_resolved_outcome(market)
            if winning_outcome is None:
                continue
            resolved[slug] = {
                "market_slug": market.get("slug", slug),
                "winning_outcome": winning_outcome,
                "closed": bool(market.get("closed")),
                "closed_time": market.get("closedTime"),
                "condition_id": str(market.get("conditionId", "")),
            }
            cache[slug] = resolved[slug]
    except URLError:
        if resolved:
            return (
                resolved,
                "Using cached resolved markets only. Could not refresh closed-market resolution data.",
            )
        return (
            {},
            "Could not refresh closed-market resolution data. Final right/wrong grading is unavailable right now.",
        )
    except subprocess.CalledProcessError:
        if resolved:
            return (
                resolved,
                "Using cached resolved markets only. Could not refresh closed-market resolution data.",
            )
        return (
            {},
            "Could not refresh closed-market resolution data. Final right/wrong grading is unavailable right now.",
        )

    if cache:
        save_cache(DEFAULT_CACHE, cache)

    if missing:
        unresolved_count = len(slugs - set(resolved))
        if unresolved_count:
            return (
                resolved,
                f"Resolved grading is partial. Missing {unresolved_count:,} market resolutions from direct market lookups.",
            )
    return resolved, None


def classify_alert_follow_through(records: list[dict]) -> tuple[list[dict], str | None]:
    evaluations = load_evaluations(DEFAULT_EVALUATIONS)
    follow_map = evaluations["follow_through"]
    if follow_map:
        graded: list[dict] = []
        for record in records:
            if record.get("status") != "alert":
                continue
            key = str(record.get("record_key") or record_key(record))
            evaluation = follow_map.get(key)
            if not evaluation:
                continue
            enriched = dict(record)
            enriched["grade"] = str(evaluation.get("grade", "unresolved"))
            if "current_price" in evaluation:
                enriched["current_price"] = evaluation["current_price"]
            if "move_pct" in evaluation:
                enriched["move_pct"] = evaluation["move_pct"]
            graded.append(enriched)
        return graded, None

    now = time.time()
    eligible_alerts = [
        record
        for record in records
        if record.get("status") == "alert"
        and (now - float(record.get("recorded_at", 0))) >= FOLLOW_THROUGH_HOURS * 3600
    ]

    if not eligible_alerts:
        return [], None

    market_map = load_active_market_map()
    if not market_map:
        return (
            [],
            "Could not refresh current Polymarket market prices. Follow-through grading is unavailable right now.",
        )
    graded: list[dict] = []
    for record in eligible_alerts:
        market = market_map.get(str(record.get("market_id", "")))
        enriched = dict(record)
        if not market:
            enriched["grade"] = "unresolved"
            graded.append(enriched)
            continue

        current_price = current_outcome_price(market, str(record.get("outcome", "")))
        if current_price is None:
            enriched["grade"] = "unresolved"
            graded.append(enriched)
            continue

        entry_price = float(record.get("price", 0))
        move = current_price - entry_price
        enriched["current_price"] = current_price
        enriched["move_pct"] = move
        if move >= FOLLOW_THROUGH_MOVE:
            enriched["grade"] = "right"
        elif move <= -FOLLOW_THROUGH_MOVE:
            enriched["grade"] = "wrong"
        else:
            enriched["grade"] = "flat"
        graded.append(enriched)

    return graded, None


def classify_resolved_alerts(records: list[dict]) -> tuple[list[dict], str | None]:
    evaluations = load_evaluations(DEFAULT_EVALUATIONS)
    resolved_map = evaluations["resolved"]
    if resolved_map:
        graded: list[dict] = []
        for record in records:
            if record.get("status") != "alert":
                continue
            key = str(record.get("record_key") or record_key(record))
            evaluation = resolved_map.get(key)
            enriched = dict(record)
            if not evaluation:
                enriched["resolved_grade"] = "unresolved"
            else:
                enriched["resolved_grade"] = str(evaluation.get("grade", "unresolved"))
                enriched["winning_outcome"] = str(evaluation.get("winning_outcome", ""))
            graded.append(enriched)
        return graded, None

    alert_records = [record for record in records if record.get("status") == "alert"]
    if not alert_records:
        return [], None

    market_slugs = {
        str(record.get("market_lookup_slug") or record.get("market_slug", ""))
        for record in alert_records
        if record.get("market_lookup_slug") or record.get("market_slug")
    }
    resolution_map, resolution_error = load_closed_market_resolution_map_by_slug(market_slugs)
    graded: list[dict] = []

    for record in alert_records:
        enriched = dict(record)
        lookup_slug = str(record.get("market_lookup_slug") or record.get("market_slug", ""))
        market = resolution_map.get(lookup_slug)
        if not market:
            enriched["resolved_grade"] = "unresolved"
            graded.append(enriched)
            continue

        winning_outcome = str(market.get("winning_outcome", "")).upper()
        trade_outcome = str(record.get("outcome", "")).upper()
        trade_side = str(record.get("side", "")).upper()
        enriched["winning_outcome"] = winning_outcome

        if not winning_outcome or not trade_outcome or trade_side not in {"BUY", "SELL"}:
            enriched["resolved_grade"] = "unresolved"
        elif trade_side == "BUY":
            enriched["resolved_grade"] = "right" if trade_outcome == winning_outcome else "wrong"
        else:
            enriched["resolved_grade"] = "right" if trade_outcome != winning_outcome else "wrong"
        graded.append(enriched)

    return graded, resolution_error


def bucketize(value: float, buckets: Iterable[tuple[float, str]]) -> str:
    for threshold, label in buckets:
        if value < threshold:
            return label
    return list(buckets)[-1][1]


def svg_bar_chart(title: str, counts: dict[str, Counter]) -> str:
    labels = list(counts.keys())
    max_value = max((max(counter.values()) for counter in counts.values()), default=1)
    bar_group_width = 90
    width = max(700, len(labels) * bar_group_width + 120)
    height = 320
    baseline = 250
    scale = 170 / max_value if max_value else 1

    bars = []
    for index, label in enumerate(labels):
        x = 70 + index * bar_group_width
        alert_value = counts[label]["alert"]
        ignored_value = counts[label]["ignored"]
        alert_height = alert_value * scale
        ignored_height = ignored_value * scale
        bars.append(
            f'<rect x="{x}" y="{baseline - alert_height:.1f}" width="28" height="{alert_height:.1f}" fill="#0f766e" />'
        )
        bars.append(
            f'<rect x="{x + 34}" y="{baseline - ignored_height:.1f}" width="28" height="{ignored_height:.1f}" fill="#b45309" />'
        )
        bars.append(
            f'<text x="{x + 31}" y="{baseline + 18}" text-anchor="middle" font-size="11">{escape(label)}</text>'
        )
        bars.append(
            f'<text x="{x + 14}" y="{baseline - alert_height - 6:.1f}" text-anchor="middle" font-size="10">{alert_value}</text>'
        )
        bars.append(
            f'<text x="{x + 48}" y="{baseline - ignored_height - 6:.1f}" text-anchor="middle" font-size="10">{ignored_value}</text>'
        )

    return f"""
    <section class="chart">
      <h2>{escape(title)}</h2>
      <svg viewBox="0 0 {width} {height}" width="100%" height="{height}">
        <line x1="50" y1="{baseline}" x2="{width - 30}" y2="{baseline}" stroke="#666" />
        <text x="70" y="24" font-size="12" fill="#0f766e">Alert</text>
        <rect x="115" y="14" width="14" height="14" fill="#0f766e" />
        <text x="150" y="24" font-size="12" fill="#b45309">Ignored</text>
        <rect x="207" y="14" width="14" height="14" fill="#b45309" />
        {"".join(bars)}
      </svg>
    </section>
    """


def svg_stacked_chart(
    title: str, counts: dict[str, Counter], legend: tuple[str, ...] = GRADE_ORDER
) -> str:
    labels = list(counts.keys())
    max_value = max((sum(counter.values()) for counter in counts.values()), default=1)
    bar_group_width = 90
    width = max(700, len(labels) * bar_group_width + 120)
    height = 340
    baseline = 270
    scale = 180 / max_value if max_value else 1

    bars = []
    legend_parts = []
    legend_x = 70
    for key in legend:
        color = GRADE_COLORS[key]
        legend_parts.append(
            f'<text x="{legend_x}" y="24" font-size="12" fill="{color}">{escape(key.title())}</text>'
        )
        legend_parts.append(
            f'<rect x="{legend_x + 55}" y="14" width="14" height="14" fill="{color}" />'
        )
        legend_x += 105

    for index, label in enumerate(labels):
        x = 80 + index * bar_group_width
        running_height = 0.0
        total = sum(counts[label].values())
        for key in legend:
            value = counts[label].get(key, 0)
            segment_height = value * scale
            if segment_height <= 0:
                continue
            y = baseline - running_height - segment_height
            bars.append(
                f'<rect x="{x}" y="{y:.1f}" width="42" height="{segment_height:.1f}" fill="{GRADE_COLORS[key]}" />'
            )
            if segment_height >= 16:
                bars.append(
                    f'<text x="{x + 21}" y="{y + segment_height / 2 + 4:.1f}" text-anchor="middle" '
                    f'font-size="10" fill="white">{value}</text>'
                )
            running_height += segment_height
        bars.append(
            f'<text x="{x + 21}" y="{baseline + 18}" text-anchor="middle" font-size="11">{escape(label)}</text>'
        )
        bars.append(
            f'<text x="{x + 21}" y="{baseline - running_height - 6:.1f}" text-anchor="middle" font-size="10">{total}</text>'
        )

    return f"""
    <section class="chart">
      <h2>{escape(title)}</h2>
      <svg viewBox="0 0 {width} {height}" width="100%" height="{height}">
        <line x1="50" y1="{baseline}" x2="{width - 30}" y2="{baseline}" stroke="#666" />
        {"".join(legend_parts)}
        {"".join(bars)}
      </svg>
    </section>
    """


def top_wallet_table(counter: Counter, limit: int = 20) -> str:
    rows = []
    for wallet, count in counter.most_common(limit):
        rows.append(f"<tr><td>{escape(wallet)}</td><td>{count}</td></tr>")
    return (
        "<table><thead><tr><th>Wallet</th><th>Records</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def follow_through_table(results: Counter) -> str:
    if not results:
        return "<p>No alert records old enough to grade yet.</p>"

    rows = []
    for key in ("right", "wrong", "flat", "unresolved"):
        rows.append(f"<tr><td>{escape(key.title())}</td><td>{results.get(key, 0)}</td></tr>")
    return (
        "<table><thead><tr><th>Outcome</th><th>Count</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def unavailable_table(message: str) -> str:
    return f"<p>{escape(message)}</p>"


def wallet_accuracy_table(counter: dict[str, Counter], limit: int = 15) -> str:
    ranked = sorted(
        counter.items(),
        key=lambda item: (
            item[1].get("right", 0) + item[1].get("wrong", 0),
            item[1].get("right", 0),
        ),
        reverse=True,
    )
    rows = []
    for wallet, results in ranked[:limit]:
        resolved = results.get("right", 0) + results.get("wrong", 0)
        accuracy = (results.get("right", 0) / resolved * 100.0) if resolved else 0.0
        rows.append(
            "<tr>"
            f"<td>{escape(wallet)}</td>"
            f"<td>{results.get('right', 0)}</td>"
            f"<td>{results.get('wrong', 0)}</td>"
            f"<td>{results.get('flat', 0)}</td>"
            f"<td>{results.get('unresolved', 0)}</td>"
            f"<td>{accuracy:.1f}%</td>"
            "</tr>"
        )
    if not rows:
        return "<p>No graded alerts yet.</p>"
    return (
        "<table><thead><tr><th>Wallet</th><th>Right</th><th>Wrong</th><th>Flat</th>"
        "<th>Unresolved</th><th>Resolved Accuracy</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def build_report(records: list[dict]) -> str:
    status_counts = Counter(record.get("status", "unknown") for record in records)
    confidence_buckets: dict[str, Counter] = defaultdict(Counter)
    size_buckets: dict[str, Counter] = defaultdict(Counter)
    wallet_counts = Counter(
        record.get("wallet_label") or record.get("wallet", "unknown") for record in records
    )
    resolved_alerts, resolved_error = classify_resolved_alerts(records)
    resolved_counts = Counter(
        record.get("resolved_grade", "unresolved") for record in resolved_alerts
    )
    resolved_confidence_buckets: dict[str, Counter] = defaultdict(Counter)
    resolved_size_buckets: dict[str, Counter] = defaultdict(Counter)
    resolved_wallets: dict[str, Counter] = defaultdict(Counter)
    graded_alerts, grading_error = classify_alert_follow_through(records)
    follow_through = Counter(record.get("grade", "unresolved") for record in graded_alerts)
    follow_confidence_buckets: dict[str, Counter] = defaultdict(Counter)
    follow_size_buckets: dict[str, Counter] = defaultdict(Counter)
    follow_wallets: dict[str, Counter] = defaultdict(Counter)

    confidence_labels = [
        (50, "<50%"),
        (60, "50-59%"),
        (70, "60-69%"),
        (80, "70-79%"),
        (90, "80-89%"),
        (101, "90%+"),
    ]
    size_labels = [
        (1000, "<$1k"),
        (5000, "$1k-$5k"),
        (15000, "$5k-$15k"),
        (50000, "$15k-$50k"),
        (100000, "$50k-$100k"),
        (float("inf"), "$100k+"),
    ]

    for record in records:
        status = record.get("status", "unknown")
        confidence = float(record.get("confidence_pct", 0))
        size_usd = float(record.get("size_usd", 0))
        confidence_bucket = bucketize(confidence, confidence_labels)
        size_bucket = bucketize(size_usd, size_labels)
        confidence_buckets[confidence_bucket][status] += 1
        size_buckets[size_bucket][status] += 1

    for record in resolved_alerts:
        grade = str(record.get("resolved_grade", "unresolved"))
        confidence = float(record.get("confidence_pct", 0))
        size_usd = float(record.get("size_usd", 0))
        wallet = record.get("wallet_label") or record.get("wallet", "unknown")
        confidence_bucket = bucketize(confidence, confidence_labels)
        size_bucket = bucketize(size_usd, size_labels)
        resolved_confidence_buckets[confidence_bucket][grade] += 1
        resolved_size_buckets[size_bucket][grade] += 1
        resolved_wallets[str(wallet)][grade] += 1

    for record in graded_alerts:
        grade = str(record.get("grade", "unresolved"))
        confidence = float(record.get("confidence_pct", 0))
        size_usd = float(record.get("size_usd", 0))
        wallet = record.get("wallet_label") or record.get("wallet", "unknown")
        confidence_bucket = bucketize(confidence, confidence_labels)
        size_bucket = bucketize(size_usd, size_labels)
        follow_confidence_buckets[confidence_bucket][grade] += 1
        follow_size_buckets[size_bucket][grade] += 1
        follow_wallets[str(wallet)][grade] += 1

    summary = f"""
    <section class="summary">
      <div class="metric"><span>Total records</span><strong>{len(records):,}</strong></div>
      <div class="metric"><span>Alerts</span><strong>{status_counts["alert"]:,}</strong></div>
      <div class="metric"><span>Ignored</span><strong>{status_counts["ignored"]:,}</strong></div>
      <div class="metric"><span>Resolved alerts graded</span><strong>{sum(resolved_counts.values()):,}</strong></div>
      <div class="metric"><span>Graded alerts</span><strong>{sum(follow_through.values()):,}</strong></div>
    </section>
    """

    resolved_available = bool(resolved_alerts) or not resolved_error
    resolved_note = (
        "<p>This section grades alerts using closed Polymarket markets. The winning outcome is inferred "
        "from final outcome prices on closed markets: winning shares settle to $1 and losing shares to $0, "
        "so a resolved market with one outcome at 1.00 and the other at 0.00 can be graded as actually right or wrong.</p>"
        if not resolved_error
        else f"<p>{escape(resolved_error)}</p>"
    )

    grading_note = (
        f"<p>{escape(grading_error)}</p>"
        if grading_error
        else (
            f"<p>This is a provisional correctness measure. It marks alerts as right/wrong based on whether the "
            f"market price moved at least {int(FOLLOW_THROUGH_MOVE * 100)} percentage points in the trade's favor "
            f"after at least {FOLLOW_THROUGH_HOURS} hours. It is not final market resolution accuracy.</p>"
        )
    )

    note = f"""
    <section class="note">
      <h2>Follow-Through Accuracy</h2>
      {follow_through_table(follow_through)}
      {grading_note}
    </section>
    <section class="note">
      <h2>Interpretation Note</h2>
      <p>This report can visualize alert frequency versus bot confidence and bet size immediately.
      The right/wrong section above uses current market follow-through for older alerts, not final outcomes.</p>
    </section>
    """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Trade Audit Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 32px; color: #111; background: #f7f7f7; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .summary {{ display: flex; gap: 16px; margin: 20px 0 28px; }}
    .metric {{ background: white; padding: 16px 20px; border-radius: 12px; min-width: 180px; box-shadow: 0 1px 2px rgba(0,0,0,.08); }}
    .metric span {{ display: block; color: #666; font-size: 13px; margin-bottom: 6px; }}
    .metric strong {{ font-size: 28px; }}
    .chart, .note, .wallets {{ background: white; padding: 20px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 1px 2px rgba(0,0,0,.08); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #e5e5e5; }}
    th {{ color: #666; font-size: 13px; }}
  </style>
</head>
<body>
  <h1>Trade Audit Report</h1>
  <p>Input: {escape(str(DEFAULT_INPUT))}</p>
  {summary}
  {svg_bar_chart("Alerts vs Ignored by Bot Confidence", confidence_buckets)}
  {svg_bar_chart("Alerts vs Ignored by Trade Size", size_buckets)}
  <section class="note">
    <h2>Actual Resolved Accuracy</h2>
    {follow_through_table(resolved_counts) if resolved_available else unavailable_table("Resolved accuracy is unavailable for this report build. The analyzer could not fetch closed-market resolution data, so these results are not being counted as unresolved trades.")}
    {resolved_note}
  </section>
  {svg_stacked_chart("Resolved Right/Wrong by Bot Confidence", resolved_confidence_buckets, legend=("right", "wrong", "unresolved")) if resolved_available else ""}
  {svg_stacked_chart("Resolved Right/Wrong by Trade Size", resolved_size_buckets, legend=("right", "wrong", "unresolved")) if resolved_available else ""}
  <section class="wallets">
    <h2>Wallet Accuracy on Resolved Alerts</h2>
    {wallet_accuracy_table(resolved_wallets) if resolved_available else unavailable_table("No resolved wallet grading available in this report build.")}
  </section>
  {svg_stacked_chart("Alert Follow-Through by Bot Confidence", follow_confidence_buckets)}
  {svg_stacked_chart("Alert Follow-Through by Trade Size", follow_size_buckets)}
  <section class="wallets">
    <h2>Most Active Wallets in Audit Log</h2>
    {top_wallet_table(wallet_counts)}
  </section>
  <section class="wallets">
    <h2>Wallet Accuracy on Graded Alerts</h2>
    {wallet_accuracy_table(follow_wallets)}
  </section>
  {note}
</body>
</html>
"""


def main() -> int:
    records = load_records(DEFAULT_INPUT)
    if not records:
        print(f"No records found in {DEFAULT_INPUT}")
        return 1
    report = build_report(records)
    DEFAULT_OUTPUT.write_text(report, encoding="utf-8")
    print(f"Wrote report to {DEFAULT_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
