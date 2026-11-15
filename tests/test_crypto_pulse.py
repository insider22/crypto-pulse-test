import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crypto_pulse import (  # noqa: E402
    build_snapshot, fetch_prices, iso, render_history, replace_readme, update, validate_config,
)


NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
CONFIG = {
    "alert_threshold_pct": 5,
    "coins": [
        {"id": "bitcoin", "symbol": "BTC", "name": "Bitcoin"},
        {"id": "ethereum", "symbol": "ETH", "name": "Ethereum"},
        {"id": "the-open-network", "symbol": "TON", "name": "Toncoin"},
    ],
}


def payload():
    return {
        "bitcoin": {"usd": 65000, "usd_24h_change": 5, "last_updated_at": NOW.timestamp()},
        "ethereum": {"usd": 1900, "usd_24h_change": -5, "last_updated_at": (NOW - timedelta(minutes=10)).timestamp()},
        "the-open-network": {"usd": 1.01, "usd_24h_change": 0.4, "last_updated_at": NOW.timestamp()},
    }


class FeedTests(unittest.TestCase):
    def test_threshold_is_inclusive_and_distinguishes_direction(self):
        snapshot = build_snapshot(CONFIG, payload(), NOW)
        self.assertEqual([c["alert"] for c in snapshot["coins"]], ["up", "down", None])
        self.assertEqual([c["symbol"] for c in snapshot["alerts"]], ["BTC", "ETH"])

    def test_expiry_uses_oldest_provider_timestamp(self):
        snapshot = build_snapshot(CONFIG, payload(), NOW)
        self.assertEqual(snapshot["generated_at"], "2026-11-15T12:00:00Z")
        self.assertEqual(snapshot["expires_at"], "2026-11-15T13:50:00Z")
        self.assertEqual(snapshot["coins"][1]["source_updated_at"], "2026-11-15T11:50:00Z")

    def test_missing_coin_does_not_publish_partial_feed(self):
        prices = payload()
        del prices["the-open-network"]
        with self.assertRaisesRegex(ValueError, "missing quote"):
            build_snapshot(CONFIG, prices, NOW)

    def test_invalid_prices_and_changes_are_rejected(self):
        for field, value in [("usd", 0), ("usd", -1), ("usd", True), ("usd", float("nan")),
                             ("usd", float("inf")), ("usd", "65000"), ("usd_24h_change", None),
                             ("usd_24h_change", -101), ("usd_24h_change", float("nan"))]:
            with self.subTest(field=field, value=value):
                prices = payload()
                prices["bitcoin"][field] = value
                with self.assertRaises(ValueError):
                    build_snapshot(CONFIG, prices, NOW)

    def test_stale_and_future_prices_are_rejected(self):
        for delta in [timedelta(hours=-2), timedelta(minutes=10)]:
            with self.subTest(delta=delta):
                prices = payload()
                prices["bitcoin"]["last_updated_at"] = (NOW + delta).timestamp()
                with self.assertRaisesRegex(ValueError, "stale or future"):
                    build_snapshot(CONFIG, prices, NOW)

    def test_config_requires_unique_coins_and_positive_threshold(self):
        cases = []
        duplicate_id = copy.deepcopy(CONFIG)
        duplicate_id["coins"][1]["id"] = "bitcoin"
        cases.append(duplicate_id)
        duplicate_symbol = copy.deepcopy(CONFIG)
        duplicate_symbol["coins"][1]["symbol"] = "BTC"
        cases.append(duplicate_symbol)
        for threshold in [0, -5, True, "5", float("inf")]:
            cases.append({**CONFIG, "alert_threshold_pct": threshold})
        cases.extend([{**CONFIG, "coins": []}, []])
        for config in cases:
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_config(config)

    def test_invalid_symbols_and_markdown_names_are_rejected(self):
        for field, value in [("symbol", "=FORMULA"), ("symbol", "btc"), ("id", "bitcoin&ids=foo"),
                             ("name", "BTC | ETH"), ("name", "line\nbreak")]:
            config = copy.deepcopy(CONFIG)
            config["coins"][0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    validate_config(config)

    def test_history_is_trimmed_and_same_second_rerun_is_idempotent(self):
        old = build_snapshot(CONFIG, payload(), NOW)
        history = render_history("", old, NOW)
        later = NOW + timedelta(days=31)
        newer = {**old, "generated_at": iso(later)}
        history = render_history(history, newer, later)
        history = render_history(history, newer, later)
        rows = list(csv.DictReader(io.StringIO(history)))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["timestamp"] == iso(later) for row in rows))

    def test_history_retains_previous_samples_inside_window(self):
        snapshot = build_snapshot(CONFIG, payload(), NOW)
        history = render_history("", snapshot, NOW)
        later = NOW + timedelta(hours=1)
        next_snapshot = {**snapshot, "generated_at": iso(later)}
        rows = list(csv.DictReader(io.StringIO(render_history(history, next_snapshot, later))))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["timestamp"], iso(NOW))

    def test_readme_update_preserves_handwritten_content(self):
        original = "intro\n<!-- pulse:start -->\nold\n<!-- pulse:end -->\nusage\n"
        result = replace_readme(original, "new\n")
        self.assertEqual(result, original.replace("old", "new"))
        for invalid in ["missing", "<!-- pulse:end --><!-- pulse:start -->", original + "<!-- pulse:end -->"]:
            with self.assertRaises(ValueError):
                replace_readme(invalid, "new\n")

    def test_retry_temporary_failure_then_success(self):
        response = io.StringIO(json.dumps(payload()))
        opener = Mock(side_effect=[
            HTTPError("https://example.test", 429, "rate limited", {}, None), response,
        ])
        sleep = Mock()
        self.assertEqual(fetch_prices(CONFIG, opener=opener, sleep=sleep), payload())
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(15)

    def test_nonretryable_error_is_not_retried(self):
        opener = Mock(side_effect=HTTPError("https://example.test", 404, "not found", {}, None))
        sleep = Mock()
        with self.assertRaises(HTTPError):
            fetch_prices(CONFIG, opener=opener, sleep=sleep)
        self.assertEqual(opener.call_count, 1)
        sleep.assert_not_called()

    def test_network_retries_are_bounded(self):
        opener, sleep = Mock(side_effect=URLError("offline")), Mock()
        with self.assertRaises(URLError):
            fetch_prices(CONFIG, opener=opener, sleep=sleep)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [15, 30])

    def test_failure_keeps_last_successful_files_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "watchlist.json").write_text(json.dumps(CONFIG), encoding="utf-8")
            (root / "README.md").write_text("intro\n<!-- pulse:start -->\n<!-- pulse:end -->\n", encoding="utf-8")
            update(root, fetch=lambda _: payload(), now=NOW)
            paths = [root / "README.md", *sorted((root / "data").glob("*"))]
            before = {p: p.read_bytes() for p in paths}

            # Bad upstream data must not turn a healthy feed into a partial one.
            with self.assertRaises(ValueError):
                update(root, fetch=lambda _: {"bitcoin": {}}, now=NOW)
            self.assertEqual(before, {p: p.read_bytes() for p in paths})

            # Network failure must not replace data with an empty response.
            def offline(_):
                raise URLError("offline")
            with self.assertRaises(URLError):
                update(root, fetch=offline, now=NOW)
            self.assertEqual(before, {p: p.read_bytes() for p in paths})

            # Broken README markers must be detected before writing any data.
            (root / "README.md").write_text("broken markers", encoding="utf-8")
            with self.assertRaises(ValueError):
                update(root, fetch=lambda _: payload(), now=NOW)
            self.assertTrue(all(p.read_bytes() == before[p] for p in paths if p.name != "README.md"))


if __name__ == "__main__":
    unittest.main()
