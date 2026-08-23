#!/usr/bin/env python3
"""Archive complete card payloads for the five newest ETB expansions.

Each card endpoint response is flattened into CSV columns. Arrays remain JSON
text, so no data returned for a card is discarded. The program prints the
number of actual RapidAPI HTTP requests, including retries.
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
RESOURCES_DIR = ROOT / "resources"
API_BASE = "https://pokemon-tcg-api.p.rapidapi.com"
EXPANSION_COUNT = 5
CARDS_PER_PAGE = 100
REQUEST_INTERVAL_SECONDS = 7
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
_last_request_at = 0.0
request_count = 0


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
    """Fetch an API response and count every outgoing HTTP request."""
    global _last_request_at, request_count
    key = os.environ.get("RAPIDAPI_KEY")
    if not key or key == "replace-with-your-rapidapi-key":
        raise RuntimeError("Missing RAPIDAPI_KEY. Create .env from .env.example first.")
    request = Request(
        f"{API_BASE}{path}?{urlencode(params)}",
        headers={"x-rapidapi-key": key, "x-rapidapi-host": "pokemon-tcg-api.p.rapidapi.com"},
    )
    for attempt in range(1, 4):
        wait = REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        request_count += 1
        try:
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


def newest_expansions() -> list[dict]:
    """Get the five latest released unique expansions that have an ETB."""
    expansions: dict[int, dict] = {}
    page = 1
    while len(expansions) < EXPANSION_COUNT:
        response = api_get("/products", {"search": "Elite Trainer Box", "sort": "episode_newest", "page": page})
        batch = response.get("data", [])
        for product in batch:
            if "elite trainer box" not in str(product.get("name", "")).lower():
                continue
            episode = product.get("episode") or {}
            try:
                episode_id = int(episode["id"])
                released_at = date.fromisoformat(str(episode["released_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            if released_at <= date.today():
                expansions[episode_id] = episode
        paging = response.get("paging", {})
        if len(expansions) >= EXPANSION_COUNT:
            break
        if not batch or page >= int(paging.get("total", page)):
            break
        page += 1
    selected = sorted(expansions.values(), key=lambda item: str(item["released_at"]), reverse=True)[:EXPANSION_COUNT]
    if len(selected) != EXPANSION_COUNT:
        raise RuntimeError(f"Only found {len(selected)} released ETB expansions; expected {EXPANSION_COUNT}.")
    return selected


def expansion_cards(episode_id: int) -> list[dict]:
    cards: list[dict] = []
    page = 1
    while True:
        response = api_get(f"/episodes/{episode_id}/cards", {"per_page": CARDS_PER_PAGE, "page": page})
        batch = response.get("data", [])
        cards.extend(batch)
        paging = response.get("paging", {})
        if not batch or page >= int(paging.get("total", page)):
            return cards
        page += 1


def safe_slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "expansion"


def flatten(value: object, prefix: str = "", result: dict[str, object] | None = None) -> dict[str, object]:
    """Flatten objects; preserve every array exactly as compact JSON text."""
    if result is None:
        result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(item, f"{prefix}.{key}" if prefix else str(key), result)
    elif isinstance(value, list):
        result[prefix] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        result[prefix] = value
    return result


def write_csv(expansion: dict, cards: list[dict], captured_at: datetime) -> Path:
    released_at = date.fromisoformat(str(expansion["released_at"]))
    name = safe_slug(expansion.get("slug") or expansion.get("name"))
    destination = RESOURCES_DIR / f"{released_at:%Y%m%d}-{name}.csv"
    rows = [flatten(card, result={"captured_at": captured_at.isoformat()}) for card in cards]
    fields = ["captured_at"] + sorted({key for row in rows for key in row} - {"captured_at"})
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return destination


def main() -> int:
    load_dotenv()
    captured_at = datetime.now().astimezone().replace(microsecond=0)
    RESOURCES_DIR.mkdir(exist_ok=True)
    for expansion in newest_expansions():
        cards = expansion_cards(int(expansion["id"]))
        if not cards:
            raise RuntimeError(f"Expansion {expansion.get('name')} has no downloadable cards.")
        path = write_csv(expansion, cards, captured_at)
        print(f"Created {path.relative_to(ROOT)}: {len(cards)} cards.")
    print(f"RapidAPI HTTP requests: {request_count}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"RapidAPI HTTP requests: {request_count}", file=sys.stderr)
        raise SystemExit(1)
