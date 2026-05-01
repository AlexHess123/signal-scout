# Signal Scout Submission Brief

## One-Sentence Pitch

Signal Scout is a self-evaluating real-time intelligence system that detects unusual whale activity in prediction markets, explains why the signal matters, and measures whether those alerts were actually right.

## Short Description

Signal Scout monitors top prediction-market traders in real time, aggregates fragmented fills into coherent position events, scores them using wallet quality, size, context, and timing, and sends alerts only when the signal looks actionable. Every alert and ignored trade is logged, and the system persists evaluation data so signals can later be judged as right or wrong.

## What Makes It Strong

- Real-time public market data ingestion
- Whale-trade aggregation from fragmented fills
- Multi-factor signal scoring
- Cloud deployment with persistent storage
- Phone notifications
- Durable audit log
- Self-evaluation infrastructure

## Demo Flow

1. Show the live worker running on Fly.
2. Show live logs with `seen`, `ignored`, and `alert`.
3. Show one phone alert.
4. Show the persistent audit/evaluation files.
5. Show the report and explain how signals are reviewed after the fact.

## Framing Guidance

Describe it as:

- market intelligence
- signal detection
- decision support
- self-evaluating analytics

Do not describe it as:

- gambling automation
- copy trading
- an insider trading detector
- an auto-betting system

## Honest Limitation Statement

The project is already fully functional as a live monitoring and alerting system, but the long-term evaluation layer is still improving as more post-fix records accumulate. That is an engineering limitation, not a product-idea limitation, and the architecture is already in place to close the loop.

## 30-Second Demo Script

`Prediction markets are noisy, and top traders often execute large positions through many small fills. Signal Scout watches those traders in real time, aggregates fragmented activity into whale-position signals, scores each event by size, timing, and wallet quality, and sends alerts only when the pattern looks actionable. The important part is that it also stores every decision and evaluates its own alerts over time, so it becomes a measurable intelligence system instead of just a notification bot.`

## 90-Second Demo Script

`Signal Scout is a real-time market intelligence system for prediction markets. The core problem is that raw market activity is noisy: top traders make thousands of executions, and most of them are not meaningful on their own. This system solves that by monitoring leaderboard traders in real time, pulling their public trade activity, aggregating split fills into coherent position events, and scoring those events using wallet quality, size, timing, clustering, and inferred intent.`

`When a trade looks like a true whale signal, the system sends a phone alert with the market, side, size, confidence, fill count, and reasons it fired. But the more important engineering piece is the feedback loop: every alert and every ignored trade is written to a durable audit log, and the worker persists evaluation records over time so the signals can later be judged as right or wrong.`

`So this is not an auto-betting bot. It is a self-auditing decision-support system designed to turn raw market flow into structured, testable signals.`
