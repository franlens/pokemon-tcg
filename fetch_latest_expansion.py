#!/usr/bin/env python3
"""Archive Cardmarket EUR prices for cards in the newest Pokemon TCG expansion.

Consumes one API request to identify the newest expansion, then one request per
100 cards in that expansion. A fresh CSV is intentionally created on every run.
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "snapshot"
API_BASE = "https://pokemon-tcg-api.p.rapidapi.com"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_MAX_ATTEMPTS = 3
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


def load_dotenv() -> None:
    """Load local .env without adding a third-party dependency."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_get_legacy(path: str, params: dict[str, object]) -> dict:
    key = os.environ.get("RAPIDAPI_KEY")
    if not key or key == "replace-with-your-rapidapi-key":
        raise RuntimeError("Falta RAPIDAPI_KEY. Crea .env desde .env.example y añade tu clave.")
    url = f"{API_BASE}{path}?{urlencode(params)}"
    request = Request(url, headers={"x-rapidapi-key": key, "x-rapidapi-host": "pokemon-tcg-api.p.rapidapi.com"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API respondió HTTP {exc.code}: {message[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"No se pudo contactar con la API: {exc.reason}") from exc


def positive_int_setting(name: str, default: int) -> int:
    """Read a positive integer environment setting, falling back safely."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Use the provider's requested wait when present, otherwise back off."""
    if retry_after:
        try:
            return min(max(float(retry_after), 0), 60)
        except ValueError:
            pass
    return min(2 ** (attempt - 1), 20)


def api_get(path: str, params: dict[str, object]) -> dict:
    """Fetch JSON, tolerating transient RapidAPI slowdowns and rate limits."""
    key = os.environ.get("RAPIDAPI_KEY")
    if not key or key == "replace-with-your-rapidapi-key":
        raise RuntimeError("Missing RAPIDAPI_KEY. Create .env from .env.example and add the key.")

    url = f"{API_BASE}{path}?{urlencode(params)}"
    timeout = positive_int_setting("RAPIDAPI_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    max_attempts = positive_int_setting("RAPIDAPI_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    request = Request(url, headers={"x-rapidapi-key": key, "x-rapidapi-host": "pokemon-tcg-api.p.rapidapi.com"})

    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == max_attempts:
                raise RuntimeError(f"API returned HTTP {exc.code}: {message[:500]}") from exc
            delay = retry_delay(attempt, exc.headers.get("Retry-After"))
            reason = f"HTTP {exc.code}"
        except (URLError, TimeoutError, socket.timeout, ConnectionError, json.JSONDecodeError) as exc:
            if attempt == max_attempts:
                raise RuntimeError(f"Could not contact the API after {max_attempts} attempts: {exc}") from exc
            delay = retry_delay(attempt)
            reason = str(exc.reason) if isinstance(exc, URLError) else str(exc)

        print(
            f"Warning: API unavailable ({reason}); retrying in {delay:g}s "
            f"({attempt}/{max_attempts - 1}).",
            file=sys.stderr,
        )
        time.sleep(delay)

    raise AssertionError("Retry loop should have returned or raised.")


def newest_expansion() -> dict:
    # The API exposes `episode_newest` for cards. One card is enough to identify
    # the newest expansion and avoids paginating every historical expansion.
    response = api_get("/cards", {"sort": "episode_newest", "per_page": 1, "page": 1})
    cards = response.get("data", [])
    if not cards or not cards[0].get("episode"):
        raise RuntimeError("La API no devolvió una expansión reciente.")
    return cards[0]["episode"]


def expansion_cards(episode_id: int) -> list[dict]:
    page = 1
    cards: list[dict] = []
    while True:
        response = api_get(f"/episodes/{episode_id}/cards", {"sort": "price_highest", "per_page": 100, "page": page})
        batch = response.get("data", [])
        cards.extend(batch)
        paging = response.get("paging", {})
        if not batch or page >= int(paging.get("total", page)):
            return cards
        page += 1


def numeric(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "expansion"


def output_path(expansion: dict, captured_at: datetime) -> Path:
    base = f"{safe_slug(expansion.get('slug') or expansion.get('name', 'expansion'))}-{captured_at:%Y-%m-%d}"
    candidate = DATA_DIR / f"{base}.csv"
    if not candidate.exists():
        return candidate
    return DATA_DIR / f"{base}-{captured_at:%H%M%S}.csv"


def flatten(value: object, prefix: str = "", result: dict[str, object] | None = None) -> dict[str, object]:
    """Flatten objects into CSV columns; retain arrays verbatim as JSON text."""
    if result is None:
        result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            flatten(item, child, result)
    elif isinstance(value, list):
        result[prefix] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        result[prefix] = value
    return result


def write_csv(cards: list[dict], expansion: dict, captured_at: datetime, field: str) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    destination = output_path(expansion, captured_at)
    rows: list[dict[str, object]] = []
    for card in cards:
        row = {"captured_at": captured_at.isoformat(), "sort_price_field": field}
        flatten(card, result=row)
        rows.append(row)
    rows.sort(key=lambda row: (numeric(row.get(f"prices.cardmarket.{field}")) is None, -(numeric(row.get(f"prices.cardmarket.{field}")) or 0)))
    fieldnames = ["captured_at", "sort_price_field"] + sorted({key for row in rows for key in row} - {"captured_at", "sort_price_field"})
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    return destination


def git_publish(file_path: Path) -> None:
    relative = file_path.relative_to(ROOT)
    subprocess.run(["git", "add", str(relative)], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-m", f"Archive {file_path.stem}"], cwd=ROOT, check=True)
    subprocess.run(["git", "push"], cwd=ROOT, check=True)


def main() -> int:
    load_dotenv()
    field = os.environ.get("CARDMARKET_PRICE_FIELD", "lowest_near_mint")
    expansion = newest_expansion()
    cards = expansion_cards(int(expansion["id"]))
    if not cards:
        raise RuntimeError("La expansión no contiene cartas descargables.")
    # The host is configured for Europe/Madrid. Using the host timezone avoids
    # requiring the separate tzdata package on Windows.
    captured_at = datetime.now().astimezone().replace(microsecond=0)
    file_path = write_csv(cards, expansion, captured_at, field)
    git_publish(file_path)
    print(f"Publicado {file_path.name}: {len(cards)} cartas de {expansion.get('name')}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
