from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


REAL_SAVED_SEARCH_SOURCES = ("cian", "yandex_realty")
DEFAULT_ALLOWED_METRO_STATIONS = ["Example Station"]

DEFAULT_SAVED_SEARCH_CONFIG: dict[str, Any] = {
    "polygon": None,
    "property_types": ["studio", "one_room"],
    "min_area": 18,
    "max_area": 50,
    "max_price": 90000,
    "include_unverified_locations": True,
    "location_filter_mode": "metro",
    "allowed_metro_stations": DEFAULT_ALLOWED_METRO_STATIONS,
    "max_metro_minutes": 20,
    "enabled_sources": list(REAL_SAVED_SEARCH_SOURCES),
    "output_directory": "output/saved-search",
}


def default_saved_search_config() -> dict[str, Any]:
    """Return a new copy so callers cannot mutate the shared defaults."""
    return deepcopy(DEFAULT_SAVED_SEARCH_CONFIG)


def load_saved_search_config(config_path: Path) -> dict[str, Any]:
    """Load a preset, falling back to defaults when no usable file exists."""
    config = default_saved_search_config()

    if not config_path.exists():
        return config

    try:
        saved_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config

    if not isinstance(saved_config, dict):
        return config

    for key in (
        "polygon",
        "property_types",
        "min_area",
        "max_area",
        "max_price",
        "include_unverified_locations",
        "location_filter_mode",
        "allowed_metro_stations",
        "max_metro_minutes",
        "output_directory",
    ):
        if key in saved_config:
            config[key] = saved_config[key]

    # This mode always runs every real source. Test data is never persisted.
    config["enabled_sources"] = list(REAL_SAVED_SEARCH_SOURCES)
    return config


def save_saved_search_config(
    config_path: Path,
    search_preset: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist a preset with an atomic same-directory replacement."""
    config = default_saved_search_config()
    config.update(
        {
            "polygon": search_preset["polygon"],
            "property_types": list(search_preset["property_types"]),
            "min_area": search_preset["min_area"],
            "max_area": search_preset["max_area"],
            "max_price": search_preset["max_price"],
            "include_unverified_locations": search_preset[
                "include_unverified_locations"
            ],
        }
    )
    config["enabled_sources"] = list(REAL_SAVED_SEARCH_SOURCES)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_name(
        f".{config_path.name}.{uuid4().hex}.tmp"
    )

    try:
        temporary_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, config_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return config
