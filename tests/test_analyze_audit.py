from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import analyze_audit


class RecordLoadingTests(unittest.TestCase):
    def test_load_records_skips_blank_and_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            path.write_text('{"status":"alert"}\nnot-json\n\n', encoding="utf-8")

            self.assertEqual(analyze_audit.load_records(path), [{"status": "alert"}])


class NetworkSafetyTests(unittest.TestCase):
    def test_http_json_rejects_non_https_urls(self) -> None:
        with self.assertRaises(ValueError):
            analyze_audit.http_json("file:///tmp/untrusted.json")

    def test_curl_fallback_has_protocol_and_time_limits(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"ok": True}), stderr=""
        )
        with (
            patch.object(analyze_audit, "urlopen", side_effect=URLError("offline")),
            patch.object(analyze_audit.subprocess, "run", return_value=completed) as run,
        ):
            result = analyze_audit.http_json("https://example.com/data")

        self.assertEqual(result, {"ok": True})
        command = run.call_args.args[0]
        self.assertIn("--connect-timeout", command)
        self.assertIn("--max-time", command)
        self.assertIn("=https", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 35)


class HtmlSafetyTests(unittest.TestCase):
    def test_wallet_table_escapes_untrusted_labels(self) -> None:
        table = analyze_audit.top_wallet_table(
            analyze_audit.Counter({"<script>alert(1)</script>": 1})
        )

        self.assertNotIn("<script>", table)
        self.assertIn("&lt;script&gt;", table)


if __name__ == "__main__":
    unittest.main()
