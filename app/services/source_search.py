from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SEARCH_CONFIG_PATH = PROJECT_ROOT / "config" / "source_search.json"


class SourceSearchConfigError(ValueError):
    """Raised when the manual CIAN calibration is unusable."""


def save_source_search_config(
    config_path: Path,
    *,
    search_url: str,
    expected_stations: list[str],
    max_metro_minutes: int,
    created_at: datetime | None = None,
) -> dict[str, object]:
    safe_url = sanitize_cian_search_url(search_url)
    if safe_url is None or not has_cian_metro_filter(safe_url):
        raise SourceSearchConfigError("Фильтр метро не подтверждён.")
    if not expected_stations or any(not station.strip() for station in expected_stations):
        raise SourceSearchConfigError("Список станций метро имеет неверный формат.")
    if not isinstance(max_metro_minutes, int) or isinstance(max_metro_minutes, bool):
        raise SourceSearchConfigError("Лимит времени до метро имеет неверный формат.")

    timestamp = (created_at or datetime.now(timezone.utc)).isoformat()
    payload: dict[str, object] = {
        "search_url": safe_url,
        "created_at": timestamp,
        "expected_stations": list(expected_stations),
        "max_metro_minutes": max_metro_minutes,
    }
    _write_json_atomically(config_path, payload)
    return payload


def load_source_search_config(config_path: Path) -> dict[str, object] | None:
    if not config_path.exists():
        return None
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    search_url = payload.get("search_url")
    expected_stations = payload.get("expected_stations")
    max_metro_minutes = payload.get("max_metro_minutes")
    created_at = payload.get("created_at")
    safe_url = sanitize_cian_search_url(search_url) if isinstance(search_url, str) else None
    if (
        safe_url is None
        or not has_cian_metro_filter(safe_url)
        or not isinstance(expected_stations, list)
        or not expected_stations
        or any(not isinstance(station, str) or not station.strip() for station in expected_stations)
        or not isinstance(max_metro_minutes, int)
        or isinstance(max_metro_minutes, bool)
        or not isinstance(created_at, str)
    ):
        return None
    return {
        "search_url": safe_url,
        "created_at": created_at,
        "expected_stations": list(expected_stations),
        "max_metro_minutes": max_metro_minutes,
    }


def sanitize_cian_search_url(value: str) -> str | None:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "cian.ru" or hostname.endswith(".cian.ru")
    ):
        return None

    sensitive_markers = ("cookie", "authorization", "auth", "token", "session", "guid")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(marker in key.casefold() for marker in sensitive_markers)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), "")
    )


def has_cian_metro_filter(search_url: str) -> bool:
    return any(
        key.casefold().startswith("metro")
        for key, _value in parse_qsl(urlsplit(search_url).query, keep_blank_values=True)
    )


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
