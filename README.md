# Signal Scout

Signal Scout is a real-time market intelligence system for prediction markets. It monitors high-signal trades from top traders, aggregates fragmented fills into coherent whale-position events, filters noise, sends alerts on unusual activity, and records everything needed to evaluate whether those alerts were actually right or wrong.

This project was built as a decision-support system. The goal is to turn raw trade flow into structured, testable signals.

## What It Does

- Tracks top Polymarket traders from the public leaderboard
- Pulls recent public trades for those traders in live mode
- Aggregates split fills into meaningful position events
- Scores trades using:
  - wallet quality
  - trade size
  - market context
  - price movement
  - trade clustering
  - inferred trade intent
- Sends phone alerts for whale-sized, high-confidence signals
- Writes a durable audit trail for every alert and ignored trade
- Persists a server-side evaluation sidecar so the system can later determine whether alerts were right or wrong

## Why It Exists

Prediction markets are noisy. Serious traders can make thousands of small executions, and most of that activity is not useful on its own.

Signal Scout is designed to answer a more valuable question:

`Which trades are meaningful enough to deserve attention right now, and do those signals actually hold up over time?`

## Architecture

Signal Scout has three layers:

1. Detect
   - ingest live public trade activity from Polymarket
   - track leaderboard traders

2. Filter
   - aggregate fragmented fills
   - score events by quality, size, timing, and context

3. Evaluate
   - store every decision in an audit log
   - persist server-side follow-through and resolved-outcome evaluations
   - generate an offline report for reviewing signal quality

## Core Files

- [bot.py](/Users/alexhess/polymarket-bot/bot.py)
  - live worker
  - scoring logic
  - aggregation
  - notifications
  - persistent audit/evaluation logging

- [analyze_audit.py](/Users/alexhess/polymarket-bot/analyze_audit.py)
  - builds the HTML report from the audit log and evaluation sidecar

- [fly.toml](/Users/alexhess/polymarket-bot/fly.toml)
  - Fly deployment configuration

## Deployment

The app is deployed as a single Fly worker with a mounted volume for persistent data.

Persistent files on the worker:

- `/data/trade_audit.jsonl`
- `/data/trade_audit_evaluations.jsonl`

## Alert Philosophy

This system is intentionally selective.

It is designed to alert on:

- large trades
- high-confidence signals
- top-trader activity
- unusual clustered behavior

It is not designed to forward every trade.

## Evaluation

Signal Scout records:

- alerts
- ignored trades
- confidence score
- reasons
- sizes
- fills
- wallet identity
- market context

It also writes an evaluation sidecar that grades alerts later using:

- short-horizon follow-through
- resolved market outcomes when available

That makes the project a self-auditing intelligence system rather than a one-way notification script.

## Limitations

- Historical alerts recorded before the `market_lookup_slug` fix are harder to resolve cleanly.
- Evaluation quality depends on market data availability and enough time passing for markets to resolve.
- The current report is strongest on post-fix records that include the correct lookup slug.
