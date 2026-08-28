from __future__ import annotations

import ssl
import sys
import unittest
from unittest.mock import patch

import bot


def event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": 100.0,
        "market_id": "market-1",
        "market_slug": "Will the test pass?",
        "market_lookup_slug": "will-the-test-pass",
        "outcome": "yes",
        "side": "buy",
        "price": 0.4,
        "size_usd": 100.0,
        "shares": 250.0,
        "wallet": "0xABC",
    }
    payload.update(overrides)
    return payload


class TradeEventTests(unittest.TestCase):
    def test_from_dict_normalizes_values(self) -> None:
        event = bot.TradeEvent.from_dict(event_payload())

        self.assertEqual(event.side, "BUY")
        self.assertEqual(event.outcome, "YES")
        self.assertEqual(event.wallet, "0xabc")

    def test_lookup_slug_falls_back_for_older_records(self) -> None:
        payload = event_payload()
        del payload["market_lookup_slug"]

        event = bot.TradeEvent.from_dict(payload)

        self.assertEqual(event.market_lookup_slug, event.market_slug)

    def test_rejects_invalid_numeric_and_side_values(self) -> None:
        for overrides in (
            {"price": 1.1},
            {"size_usd": -1},
            {"shares": -1},
            {"side": "hold"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                bot.TradeEvent.from_dict(event_payload(**overrides))


class AggregationTests(unittest.TestCase):
    def test_merges_split_fills_using_weighted_price(self) -> None:
        first = bot.TradeEvent.from_dict(event_payload())
        second = bot.TradeEvent.from_dict(
            event_payload(timestamp=110.0, price=0.6, size_usd=300.0, shares=500.0)
        )

        merged = list(bot.AggregatedEvents([first, second], window_seconds=60.0))

        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0].price, 0.55)
        self.assertEqual(merged[0].size_usd, 400.0)
        self.assertEqual(merged[0].fill_count, 2)


class SecurityBoundaryTests(unittest.TestCase):
    def test_https_validator_rejects_unsafe_urls(self) -> None:
        for value in (
            "http://example.com",
            "file:///tmp/data",
            "https://user:password@example.com",
            "not-a-url",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bot.require_https_url(value)

    def test_ntfy_notifier_encodes_topic_as_one_path_component(self) -> None:
        notifier = bot.NtfyNotifier("private/topic", ssl_context=ssl.create_default_context())

        self.assertEqual(notifier.url, "https://ntfy.sh/private%2Ftopic")

    def test_notification_secrets_are_environment_only(self) -> None:
        help_text = bot.build_parser().format_help()

        self.assertNotIn("pushover-app-token", help_text)
        self.assertNotIn("pushover-user-key", help_text)
        self.assertNotIn("ntfy-topic", help_text)

    def test_live_mode_rejects_insecure_ssl_before_network_use(self) -> None:
        argv = [
            "bot.py",
            "--mode",
            "live",
            "--notify-via",
            "stdout",
            "--allow-insecure-ssl",
        ]
        with patch.object(sys, "argv", argv):
            self.assertEqual(bot.main(), 2)


class TextSafetyTests(unittest.TestCase):
    def test_safe_text_removes_control_characters_and_caps_length(self) -> None:
        self.assertEqual(bot.safe_text("hello\nworld\x00"), "hello world")
        self.assertEqual(bot.safe_text("abcdef", max_length=4), "abc...")


if __name__ == "__main__":
    unittest.main()
