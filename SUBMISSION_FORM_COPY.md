# Submission Form Copy

## Project Title

Signal Scout

## Tagline

A self-evaluating real-time intelligence system for prediction markets

## Short Description

Signal Scout monitors top prediction-market traders in real time, aggregates fragmented fills into coherent whale-position signals, scores them using size, timing, wallet quality, and market context, and sends alerts only when the signal looks actionable.

## Full Description

Signal Scout is a real-time market intelligence system built to turn noisy trade flow into structured, testable signals. It tracks top prediction-market traders, ingests their public trade activity, aggregates split fills into meaningful position events, and scores those events using wallet quality, size, timing, clustering, and inferred intent.

The system is designed as decision support rather than auto-betting. Every alert includes explainable context, and every alert or ignored trade is written to a durable audit log. A persistent evaluation layer then grades signals over time, so the project can measure whether its alerts were actually right or wrong instead of just producing notifications.

## What Makes It Unique

- Real-time ingestion of public prediction-market trading activity
- Whale-signal detection from fragmented execution data
- Multi-factor scoring and explainable alerts
- Persistent audit logging
- Server-side evaluation sidecar for offline analysis
- Built to improve over time using measured outcomes

## How Codex Was Used

- Implemented the live trade ingestion and scoring logic
- Built the aggregation layer for fragmented fills
- Added cloud deployment configuration for Fly
- Integrated phone notifications
- Added persistent audit and evaluation logging
- Built the HTML analysis report and submission-ready project packaging

## Demo Notes

- Show the live worker running on Fly
- Show `seen`, `ignored`, and `alert` logs
- Show a phone alert example
- Show the audit log and evaluation sidecar
- Show the HTML report for post-run analysis

## Honest Limitation

The project is already fully functional as a live monitoring and alerting system, but long-term evaluation quality improves as more post-fix records and resolved markets accumulate. The architecture for that feedback loop is now in place.
