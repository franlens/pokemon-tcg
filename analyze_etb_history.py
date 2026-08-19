#!/usr/bin/env python3
"""Export six months of daily prices for the 7 leading cards of an eligible ETB.

The selected expansion is the newest one represented by a standard Elite Trainer
Box whose release date is at least ``--months`` calendar months old.  One row is
written for each card/date price observation returned by the API.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
HISTORY_DIR = ROOT / "data" / "history"
API_BASE = "https://pokemon-tcg-api.p.rapidapi.com"
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
MIN_REQUEST_INTERVAL_SECONDS = 7
TOP_CARD_COUNT = 7
GITHUB_REPOSITORY = "franlens/pokemon-tcg"
_last_request_at = 0.0


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_get(path: str, params: dict[str, object]) -> dict:
    global _last_request_at
    key = os.environ.get("RAPIDAPI_KEY")
    if not key or key == "replace-with-your-rapidapi-key":
        raise RuntimeError("Missing RAPIDAPI_KEY. Create .env from .env.example first.")
    request = Request(
        f"{API_BASE}{path}?{urlencode(params)}",
        headers={"x-rapidapi-key": key, "x-rapidapi-host": "pokemon-tcg-api.p.rapidapi.com"},
    )
    for attempt in range(1, 4):
        try:
            wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            with urlopen(request, timeout=60) as response:
                _last_request_at = time.monotonic()
                return json.load(response)
        except HTTPError as exc:
            _last_request_at = time.monotonic()
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == 3:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"API returned HTTP {exc.code}: {detail[:500]}") from exc
        except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            if attempt == 3:
                raise RuntimeError(f"API request failed after 3 attempts: {exc}") from exc
        time.sleep(2 ** (attempt - 1))
    raise AssertionError("Unreachable")


def calendar_months_before(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    # The 28th exists in every month, so this avoids an external date library.
    last_day = (date(year + (month == 12), month % 12 + 1, 1) - date.resolution).day
    return date(year, month, min(value.day, last_day))


def newest_eligible_expansion(cutoff: date) -> dict:
    """Find the first eligible ETB expansion without enumerating old products."""
    eligible: dict[int, dict] = {}
    page = 1
    while True:
        response = api_get("/products", {"search": "Elite Trainer Box", "sort": "episode_newest", "page": page})
        batch = response.get("data", [])
        releases: list[date] = []
        for product in batch:
            name = str(product.get("name", "")).lower()
            episode = product.get("episode") or {}
            released = episode.get("released_at")
            if "elite trainer box" not in name or not released:
                continue
            try:
                released_at = date.fromisoformat(str(released))
                episode_id = int(episode["id"])
            except (KeyError, TypeError, ValueError):
                continue
            releases.append(released_at)
            if released_at <= cutoff:
                eligible[episode_id] = episode
        if eligible:
            return max(eligible.values(), key=lambda episode: str(episode["released_at"]))
        paging = response.get("paging", {})
        if not batch or page >= int(paging.get("total", page)):
            break
        page += 1
    if not eligible:
        raise RuntimeError(f"No Elite Trainer Box expansion was released on or before {cutoff}.")
    return max(eligible.values(), key=lambda episode: str(episode["released_at"]))


def top_cards(episode_id: int) -> list[dict]:
    response = api_get(
        f"/episodes/{episode_id}/cards",
        {"sort": "price_highest", "per_page": TOP_CARD_COUNT, "page": 1},
    )
    cards = response.get("data", [])
    if len(cards) < TOP_CARD_COUNT:
        raise RuntimeError(f"Expansion {episode_id} only returned {len(cards)} cards; {TOP_CARD_COUNT} are required.")
    return cards[:TOP_CARD_COUNT]


def card_history(card: dict, start: date, end: date) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    page = 1
    while True:
        response = api_get(
            "/history-prices",
            {"id": card["id"], "date_from": start.isoformat(), "date_to": end.isoformat(), "sort": "asc", "page": page},
        )
        data = response.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected history response for card {card['id']}.")
        for observed_on, prices in data.items():
            row = {
                "observed_on": observed_on,
                "card_id": card["id"],
                "cardmarket_id": card.get("cardmarket_id"),
                "tcgid": card.get("tcgid"),
                "card_name": card.get("name"),
                "card_number": card.get("card_number"),
                "rarity": card.get("rarity"),
            }
            if isinstance(prices, dict):
                row.update(prices)
            rows.append(row)
        paging = response.get("paging", {})
        if not data or page >= int(paging.get("total", page)):
            return rows
        page += 1


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "expansion"


def history_filename(expansion: dict, start: date, end: date) -> str:
    slug = safe_slug(str(expansion.get("slug") or expansion["name"]))
    return f"{slug}-{start:%Y-%m-%d}-{end:%Y-%m-%d}.csv"


def github_history_exists(expansion: dict) -> str | None:
    """Return an existing remote CSV for this expansion, if GitHub has one.

    This is deliberately a remote check rather than a local filesystem check:
    a fresh cron environment must not repeat an expensive API export already
    committed by an earlier run.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/data/history?ref=master"
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "pokemon-tcg-history-job"})
    try:
        with urlopen(request, timeout=30) as response:
            entries = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Could not check GitHub history (HTTP {exc.code}): {detail[:300]}") from exc
    except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not check GitHub history: {exc}") from exc
    if not isinstance(entries, list):
        raise RuntimeError("Unexpected GitHub history directory response.")
    prefix = f"{safe_slug(str(expansion.get('slug') or expansion['name']))}-"
    for entry in entries:
        if entry.get("type") == "file" and str(entry.get("name", "")).startswith(prefix) and str(entry.get("name", "")).endswith(".csv"):
            return str(entry["name"])
    return None


def write_csv(expansion: dict, cards: list[dict], start: date, end: date) -> Path:
    rows: list[dict[str, object]] = []
    for rank, card in enumerate(cards, start=1):
        for row in card_history(card, start, end):
            row.update(
                {
                    "expansion_id": expansion["id"],
                    "expansion_name": expansion.get("name"),
                    "expansion_released_at": expansion.get("released_at"),
                    "price_rank": rank,
                    "analysis_start": start.isoformat(),
                    "analysis_end": end.isoformat(),
                }
            )
            rows.append(row)
    rows.sort(key=lambda row: (int(row["price_rank"]), str(row["observed_on"])))
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    destination = HISTORY_DIR / history_filename(expansion, start, end)
    fieldnames = [
        "expansion_id", "expansion_name", "expansion_released_at", "analysis_start", "analysis_end",
        "price_rank", "observed_on", "card_id", "cardmarket_id", "tcgid", "card_name", "card_number", "rarity",
    ]
    fieldnames += sorted({key for row in rows for key in row} - set(fieldnames))
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return destination


def publish(path: Path) -> None:
    relative = path.relative_to(ROOT)
    subprocess.run(["git", "add", str(relative)], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f"Add ETB card history {path.stem}"], cwd=ROOT, check=True)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6, help="Minimum ETB age and history window (default: 6).")
    parser.add_argument("--date-to", type=date.fromisoformat, default=date.today(), help="End date, YYYY-MM-DD (default: today).")
    parser.add_argument("--publish", action="store_true", help="Commit and push the resulting CSV.")
    args = parser.parse_args()
    if args.months < 1:
        raise ValueError("--months must be at least 1")
    load_dotenv()
    start = calendar_months_before(args.date_to, args.months)
    expansion = newest_eligible_expansion(start)
    existing_history = github_history_exists(expansion)
    if existing_history:
        print(f"Skipped: GitHub already contains data/history/{existing_history} for {expansion['name']}.")
        return 0
    cards = top_cards(int(expansion["id"]))
    output = write_csv(expansion, cards, start, args.date_to)
    if args.publish:
        publish(output)
    print(f"Created {output.relative_to(ROOT)} with {len(cards)} cards from {expansion['name']}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
