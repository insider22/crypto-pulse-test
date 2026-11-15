#!/usr/bin/env python3
"""An hourly crypto price feed. Python standard library only; no API key."""

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://api.coingecko.com/api/v3/simple/price"
MAX_QUOTE_AGE = timedelta(hours=1)
FEED_TTL = timedelta(hours=2)
HISTORY_DAYS = 30
START = "<!-- pulse:start -->"
END = "<!-- pulse:end -->"
HISTORY_FIELDS = [
    "timestamp", "symbol", "price_usd", "change_24h_pct", "source_updated_at"
]


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def number(value, field):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return float(value)


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("watchlist must be an object")
    threshold = number(config.get("alert_threshold_pct"), "alert_threshold_pct")
    if threshold <= 0:
        raise ValueError("alert_threshold_pct must be positive")
    coins = config.get("coins")
    if not isinstance(coins, list) or not 1 <= len(coins) <= 20:
        raise ValueError("watchlist must contain 1 to 20 coins")
    ids, symbols = set(), set()
    for coin in coins:
        if not isinstance(coin, dict):
            raise ValueError("each coin must be an object")
        coin_id, symbol = coin.get("id", ""), coin.get("symbol", "")
        name = coin.get("name", "")
        if not isinstance(coin_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", coin_id):
            raise ValueError("invalid CoinGecko id")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,12}", symbol):
            raise ValueError("symbol must contain 1 to 12 uppercase letters or digits")
        if not isinstance(name, str) or not name.strip() or any(c in name for c in "|\r\n<>"):
            raise ValueError("invalid display name")
        if coin_id in ids or symbol in symbols:
            raise ValueError("coin ids and symbols must be unique")
        ids.add(coin_id)
        symbols.add(symbol)
    return config


def fetch_prices(config, opener=urlopen, sleep=time.sleep):
    query = urlencode({
        "ids": ",".join(coin["id"] for coin in config["coins"]),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    })
    request = Request(f"{API_URL}?{query}", headers={
        "Accept": "application/json",
        "User-Agent": "crypto-pulse-test/1.0",
    })
    for attempt in range(3):
        try:
            with opener(request, timeout=25) as response:
                return json.load(response)
        except HTTPError as exc:
            # Retry temporary failures only, with a bounded delay.
            if exc.code != 429 and not 500 <= exc.code < 600:
                raise
            if attempt == 2:
                raise
            sleep(15 * (attempt + 1))
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            sleep(15 * (attempt + 1))
    raise RuntimeError("price fetch failed")


def build_snapshot(config, payload, now):
    validate_config(config)
    if not isinstance(payload, dict):
        raise ValueError("provider response must be an object")
    coins = []
    oldest_quote = now
    for item in config["coins"]:
        quote = payload.get(item["id"])
        if not isinstance(quote, dict):
            raise ValueError(f"missing quote for {item['id']}")
        price = number(quote.get("usd"), f"{item['symbol']} price")
        change = number(quote.get("usd_24h_change"), f"{item['symbol']} 24h change")
        timestamp = number(quote.get("last_updated_at"), f"{item['symbol']} timestamp")
        if price <= 0 or change < -100:
            raise ValueError(f"invalid price or 24h change for {item['symbol']}")
        updated = datetime.fromtimestamp(timestamp, timezone.utc)
        if now - updated > MAX_QUOTE_AGE or updated - now > timedelta(minutes=5):
            raise ValueError(f"stale or future quote for {item['symbol']}")
        oldest_quote = min(oldest_quote, updated)
        alert = None
        if abs(change) >= config["alert_threshold_pct"]:
            alert = "up" if change > 0 else "down"
        coins.append({
            **item,
            "price_usd": price,
            "change_24h_pct": change,
            "source_updated_at": iso(updated),
            "alert": alert,
        })
    return {
        "schema_version": 1,
        "generated_at": iso(now),
        "expires_at": iso(oldest_quote + FEED_TTL),
        "currency": "USD",
        "source": {"name": "CoinGecko", "url": "https://www.coingecko.com/"},
        "alert_threshold_pct": config["alert_threshold_pct"],
        "coins": coins,
        "alerts": [
            {"symbol": c["symbol"], "direction": c["alert"], "change_24h_pct": c["change_24h_pct"]}
            for c in coins if c["alert"]
        ],
    }


def render_summary(snapshot):
    lines = [
        f"Обновлено: **{snapshot['generated_at']}** (UTC). Источник: [CoinGecko](https://www.coingecko.com/).",
        "",
        "| Монета | Цена, USD | За 24 часа | Движение |",
        "| --- | ---: | ---: | --- |",
    ]
    for coin in snapshot["coins"]:
        price = f"${coin['price_usd']:,.2f}" if coin["price_usd"] >= 1 else f"${coin['price_usd']:,.6f}"
        movement = {"up": "↑ выше порога", "down": "↓ ниже порога", None: "—"}[coin["alert"]]
        lines.append(f"| {coin['symbol']} · {coin['name']} | {price} | {coin['change_24h_pct']:+.2f}% | {movement} |")
    lines.extend([
        "",
        f"Порог: **±{snapshot['alert_threshold_pct']:g}% за 24 часа**. Это сравнение за сутки у провайдера, а не с прошлым запуском.",
        "",
        f"Считать сводку устаревшей после **{snapshot['expires_at']}**. Проверяйте это поле при чтении JSON.",
    ])
    return "\n".join(lines) + "\n"


def render_history(previous, snapshot, now):
    rows = []
    cutoff = now - timedelta(days=HISTORY_DAYS)
    if previous.strip():
        reader = csv.DictReader(io.StringIO(previous))
        if reader.fieldnames != HISTORY_FIELDS:
            raise ValueError("unexpected history.csv columns")
        for row in reader:
            timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if cutoff <= timestamp < now:
                rows.append(row)
    for coin in snapshot["coins"]:
        rows.append({
            "timestamp": snapshot["generated_at"],
            "symbol": coin["symbol"],
            "price_usd": coin["price_usd"],
            "change_24h_pct": coin["change_24h_pct"],
            "source_updated_at": coin["source_updated_at"],
        })
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=HISTORY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def replace_readme(readme, summary):
    if readme.count(START) != 1 or readme.count(END) != 1 or readme.index(START) > readme.index(END):
        raise ValueError("README must contain one ordered pair of pulse markers")
    before, rest = readme.split(START, 1)
    _, after = rest.split(END, 1)
    return f"{before}{START}\n{summary}{END}{after}"


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def update(root, fetch=fetch_prices, now=None):
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    config = validate_config(json.loads((root / "watchlist.json").read_text(encoding="utf-8")))
    # Validate every quote and render every output before replacing successful data.
    snapshot = build_snapshot(config, fetch(config), now)
    summary = render_summary(snapshot)
    readme = replace_readme((root / "README.md").read_text(encoding="utf-8"), summary)
    history_path = root / "data/history.csv"
    previous = history_path.read_text(encoding="utf-8") if history_path.exists() else ""
    history = render_history(previous, snapshot, now)
    for relative, content in {
        "data/latest.json": json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        "data/history.csv": history,
        "data/summary.md": summary,
        "README.md": readme,
    }.items():
        atomic_write(root / relative, content)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        snapshot = update(args.root)
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        print(f"Crypto Pulse failed; the last successful feed is retained: {exc}", file=sys.stderr)
        return 1
    print(render_summary(snapshot), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
