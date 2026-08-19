#!/usr/bin/env python3
"""Archive Cardmarket EUR prices for a mature newest Pokémon TCG ETB expansion.

It stops unless the newest ETB expansion has been officially released for at
least 20 days. It also stops before downloading cards when GitHub already has a
snapshot for that expansion.
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
from datetime import date, datetime, timedelta
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
MINIMUM_ETB_AGE_DAYS = 20
GITHUB_REPOSITORY = "franlens/pokemon-tcg"


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


def newest_etb_expansion() -> dict:
    """Return the newest expansion represented by an Elite Trainer Box."""
    response = api_get("/products", {"search": "Elite Trainer Box", "sort": "episode_newest", "page": 1})
    expansions: dict[int, dict] = {}
    for product in response.get("data", []):
        if "elite trainer box" not in str(product.get("name", "")).lower():
            continue
        episode = product.get("episode") or {}
        try:
            expansions[int(episode["id"])] = episode
        except (KeyError, TypeError, ValueError):
            continue
    if not expansions:
        raise RuntimeError("The API returned no recent Elite Trainer Box expansion.")
    return max(expansions.values(), key=lambda episode: str(episode.get("released_at", "")))


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


def github_snapshot_exists(expansion: dict) -> str | None:
    """Return an existing GitHub snapshot for the expansion, if any."""
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/data/snapshot?ref=master"
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "pokemon-tcg-snapshot-job"})
    try:
        with urlopen(request, timeout=30) as response:
            entries = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Could not check GitHub snapshots (HTTP {exc.code}): {detail[:300]}") from exc
    except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not check GitHub snapshots: {exc}") from exc
    if not isinstance(entries, list):
        raise RuntimeError("Unexpected GitHub snapshot directory response.")
    prefix = f"{safe_slug(str(expansion.get('slug') or expansion['name']))}-"
    for entry in entries:
        name = str(entry.get("name", ""))
        if entry.get("type") == "file" and name.startswith(prefix) and name.endswith(".csv"):
            return name
    return None


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
    # The host is configured for Europe/Madrid. Using the host timezone avoids
    # requiring the separate tzdata package on Windows.
    captured_at = datetime.now().astimezone().replace(microsecond=0)
    expansion = newest_etb_expansion()
    try:
        released_at = date.fromisoformat(str(expansion["released_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("The newest ETB expansion has no valid official release date.") from exc
    available_from = released_at + timedelta(days=MINIMUM_ETB_AGE_DAYS)
    if captured_at.date() < available_from:
        print(
            f"Skipped: {expansion.get('name')} was released on {released_at} and is not eligible until {available_from}."
        )
        return 0
    existing_snapshot = github_snapshot_exists(expansion)
    if existing_snapshot:
        print(f"Skipped: GitHub already contains data/snapshot/{existing_snapshot} for {expansion.get('name')}.")
        return 0
    cards = expansion_cards(int(expansion["id"]))
    if not cards:
        raise RuntimeError("The expansion contains no downloadable cards.")
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
