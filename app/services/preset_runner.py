from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape
import json
import os
from pathlib import Path
import re
from typing import Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
import webbrowser

from app.models import Listing
from app.services.filtering import filter_listings
from app.services.browser_source_runner import (
    BrowserSearchCancelled,
    BrowserSourceDiagnostics,
    BrowserSourceRunner,
    MAX_CIAN_PAGES,
)
from app.services.source_search import (
    SOURCE_SEARCH_CONFIG_PATH,
    load_source_search_config,
)
from app.services.saved_search import (
    REAL_SAVED_SEARCH_SOURCES,
    load_saved_search_config,
)
from app.sources.base import SearchSource, SourceSearchParams, SourceSearchResult


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRESET_CONFIG_PATH = PROJECT_ROOT / "config" / "search_preset.json"
PRESET_OUTPUT_RELATIVE_PATH = Path("output") / "saved-search" / "index.html"
PRESET_DEBUG_RELATIVE_PATH = Path("debug") / "saved_search_last_run.json"

SOURCE_LABELS = {
    "cian": "ЦИАН",
    "yandex_realty": "Яндекс Недвижимость",
}
METRO_LOCATION_FILTER_MODE = "metro"
POLYGON_LOCATION_FILTER_MODE = "polygon"
WORLD_POLYGON = (
    (-180.0, -90.0),
    (180.0, -90.0),
    (180.0, 90.0),
    (-180.0, 90.0),
    (-180.0, -90.0),
)


class SavedSearchPresetError(ValueError):
    """Raised when the saved preset cannot be run safely."""


class SavedSearchCancelled(Exception):
    """Raised when the user cancels manual browser verification."""


@dataclass(frozen=True)
class SavedSourceStatus:
    source: str
    status: str


@dataclass(frozen=True)
class SavedSearchRun:
    output_path: Path
    listings: tuple[Listing, ...]
    source_statuses: tuple[SavedSourceStatus, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class MetroFilteringStatistics:
    total_unique: int
    excluded_without_metro: int
    excluded_without_metro_minutes: int
    excluded_by_station: int
    excluded_by_metro_minutes: int
    excluded_by_type: int
    excluded_by_area: int
    excluded_by_price: int
    result_count: int


async def run_saved_search(
    config_path: Path = PRESET_CONFIG_PATH,
    *,
    source_adapters: Mapping[str, SearchSource] | None = None,
    browser_source_runner: BrowserSourceRunner | None = None,
    project_root: Path = PROJECT_ROOT,
    cian_setup_config_path: Path = SOURCE_SEARCH_CONFIG_PATH,
    output_relative_path: Path | None = None,
    debug_relative_path: Path | None = None,
    report_title: str = "Rental Search Report",
    created_at: datetime | None = None,
    console: Callable[[str], None] | None = None,
) -> SavedSearchRun:
    """Run the saved preset without starting the FastAPI application."""
    config = load_saved_search_config(config_path)
    location_filter_mode = _location_filter_mode(config)
    cian_calibration = load_source_search_config(cian_setup_config_path)
    params = _search_params_from_config(config, cian_calibration)
    enabled_sources = _enabled_real_sources(config)

    collected_listings: list[Listing] = []
    source_statuses: list[SavedSourceStatus] = []
    warnings: list[str] = []
    successfully_loaded_listings = False
    active_browser_runner: BrowserSourceRunner | None = None

    if source_adapters is None:
        active_browser_runner = browser_source_runner or BrowserSourceRunner()
        try:
            results_by_source = await active_browser_runner.search_sources(
                enabled_sources,
                params,
            )
        except BrowserSearchCancelled as error:
            raise SavedSearchCancelled() from error
    else:
        results_by_source = await _search_with_adapters(
            enabled_sources,
            params,
            source_adapters,
        )

    for source_name in enabled_sources:
        result = results_by_source.get(source_name)
        if result is None:
            source_statuses.append(SavedSourceStatus(source_name, "unavailable"))
            warnings.append(_source_warning(source_name, "unavailable"))
            continue
        source_statuses.append(SavedSourceStatus(source_name, result.status))
        if result.status == "ok":
            collected_listings.extend(result.listings)
            successfully_loaded_listings = (
                successfully_loaded_listings
                or result.total_before_filtering > 0
            )
        else:
            warnings.append(_source_warning(source_name, result.status, result.error))

    if location_filter_mode == METRO_LOCATION_FILTER_MODE:
        filtered_listings, statistics = filter_listings_by_metro(
            collected_listings,
            config,
            params,
        )
        if console is not None:
            _log_metro_filtering_statistics(console, statistics)
    else:
        filtered_listings = filter_listings(collected_listings, params)
        if not config["include_unverified_locations"]:
            filtered_listings = [
                listing for listing in filtered_listings if listing.location_verified
            ]

    listings = tuple(
        sort_saved_listings(
            deduplicate_listings(filtered_listings),
            location_filter_mode=location_filter_mode,
            preferred_station=_first_configured_station(config),
        )
    )
    output_path = project_root / (
        output_relative_path or _output_relative_path(config)
    )
    if console is not None:
        console("Creating the saved-search report...")
    create_saved_search_html(
        output_path,
        listings=listings,
        config=config,
        source_statuses=source_statuses,
        warnings=warnings,
        successfully_loaded_listings=successfully_loaded_listings,
        report_title=report_title,
        created_at=created_at,
    )
    _write_saved_search_debug(
        project_root / (debug_relative_path or PRESET_DEBUG_RELATIVE_PATH),
        config=config,
        source_statuses=source_statuses,
        browser_diagnostics=(active_browser_runner.last_diagnostics if active_browser_runner else {}),
        metro_statistics=(
            statistics
            if location_filter_mode == METRO_LOCATION_FILTER_MODE
            else None
        ),
        result_count=len(listings),
    )
    if (
        console is not None
        and location_filter_mode == METRO_LOCATION_FILTER_MODE
        and len(listings) < 5
        and _cian_pages_processed(
            active_browser_runner.last_diagnostics if active_browser_runner else {}
        ) >= MAX_CIAN_PAGES
    ):
        console(
            "Подходящих вариантов найдено мало. Проверены первые 5 страниц выдачи."
        )

    return SavedSearchRun(
        output_path=output_path,
        listings=listings,
        source_statuses=tuple(source_statuses),
        warnings=tuple(warnings),
    )


def deduplicate_listings(listings: Sequence[Listing]) -> list[Listing]:
    """Merge duplicate groups and retain the most complete listing in each."""
    groups: list[list[Listing]] = []

    for listing in listings:
        matching_group_indexes = [
            index
            for index, group in enumerate(groups)
            if any(_are_duplicates(listing, candidate) for candidate in group)
        ]

        if not matching_group_indexes:
            groups.append([listing])
            continue

        combined_group = [listing]
        for index in reversed(matching_group_indexes):
            combined_group.extend(groups.pop(index))
        groups.append(combined_group)

    return [max(group, key=_listing_completeness) for group in groups]


def sort_saved_listings(
    listings: Sequence[Listing],
    *,
    location_filter_mode: str = POLYGON_LOCATION_FILTER_MODE,
    preferred_station: str | None = None,
) -> list[Listing]:
    if location_filter_mode == METRO_LOCATION_FILTER_MODE:
        return sorted(
            listings,
            key=lambda listing: _metro_listing_sort_key(
                listing,
                preferred_station=preferred_station,
            ),
        )

    return sorted(
        listings,
        key=lambda listing: (
            not listing.location_verified,
            listing.rent_price,
            listing.url,
        ),
    )


def filter_listings_by_metro(
    listings: Sequence[Listing],
    config: Mapping[str, object],
    params: SourceSearchParams,
) -> tuple[list[Listing], MetroFilteringStatistics]:
    allowed_stations = _allowed_metro_stations(config)
    max_metro_minutes = _max_metro_minutes(config)
    accepted: list[Listing] = []
    excluded_without_metro = 0
    excluded_without_metro_minutes = 0
    excluded_by_station = 0
    excluded_by_metro_minutes = 0
    excluded_by_type = 0
    excluded_by_area = 0
    excluded_by_price = 0

    for listing in listings:
        if listing.property_type not in params.property_types:
            excluded_by_type += 1
            continue
        if (
            params.min_area is not None
            and listing.area_sqm < params.min_area
        ) or (
            params.max_area is not None
            and listing.area_sqm > params.max_area
        ):
            excluded_by_area += 1
            continue
        if listing.rent_price > params.max_price:
            excluded_by_price += 1
            continue
        if listing.metro_station is None:
            excluded_without_metro += 1
            continue
        if listing.metro_minutes is None:
            excluded_without_metro_minutes += 1
            continue

        normalized_station = normalize_metro_station(listing.metro_station)
        if normalized_station not in allowed_stations:
            excluded_by_station += 1
            continue
        if listing.metro_minutes > max_metro_minutes:
            excluded_by_metro_minutes += 1
            continue
        accepted.append(listing)

    statistics = MetroFilteringStatistics(
        total_unique=_unique_listing_count(listings),
        excluded_without_metro=excluded_without_metro,
        excluded_without_metro_minutes=excluded_without_metro_minutes,
        excluded_by_station=excluded_by_station,
        excluded_by_metro_minutes=excluded_by_metro_minutes,
        excluded_by_type=excluded_by_type,
        excluded_by_area=excluded_by_area,
        excluded_by_price=excluded_by_price,
        result_count=len(accepted),
    )
    return accepted, statistics


def normalize_metro_station(value: str) -> str:
    normalized = value.casefold().replace("ё", "е")
    normalized = re.sub(
        r"^\s*(?:станция\s+метро|метро|м\.)\s*",
        "",
        normalized,
    )
    normalized = re.sub(r"[-–—]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _metro_listing_sort_key(
    listing: Listing,
    *,
    preferred_station: str | None,
) -> tuple[int, int, int, str]:
    station = normalize_metro_station(listing.metro_station or "")
    minutes = listing.metro_minutes if listing.metro_minutes is not None else 10_000
    if preferred_station and station == normalize_metro_station(preferred_station):
        return (0, minutes, listing.rent_price, listing.url)
    return (1, listing.rent_price, minutes, listing.url)


def _unique_listing_count(listings: Sequence[Listing]) -> int:
    return len(
        {
            (listing.source, listing.external_id)
            for listing in listings
        }
    )


def _log_metro_filtering_statistics(
    console: Callable[[str], None],
    statistics: MetroFilteringStatistics,
) -> None:
    console(
        f"Всего уникальных объявлений: {statistics.total_unique}."
    )
    console(f"Исключено без метро: {statistics.excluded_without_metro}.")
    console(
        "Исключено без времени до метро: "
        f"{statistics.excluded_without_metro_minutes}."
    )
    console(
        "Исключено из-за неподходящей станции: "
        f"{statistics.excluded_by_station}."
    )
    console(
        "Исключено из-за времени до метро: "
        f"{statistics.excluded_by_metro_minutes}."
    )
    console(f"Исключено по типу: {statistics.excluded_by_type}.")
    console(f"Исключено по площади: {statistics.excluded_by_area}.")
    console(f"Исключено по цене: {statistics.excluded_by_price}.")
    console(f"Итоговое количество: {statistics.result_count}.")


def _write_saved_search_debug(
    debug_path: Path,
    *,
    config: Mapping[str, object],
    source_statuses: Sequence[SavedSourceStatus],
    browser_diagnostics: Mapping[str, BrowserSourceDiagnostics],
    metro_statistics: MetroFilteringStatistics | None,
    result_count: int,
) -> Path:
    """Persist only aggregate run diagnostics, never browser profile data."""
    source_status_by_name = {
        source_status.source: source_status.status
        for source_status in source_statuses
    }
    sources: dict[str, object] = {}
    for source_name, status in source_status_by_name.items():
        diagnostics = browser_diagnostics.get(source_name)
        source_data: dict[str, object] = {"status": status}
        if diagnostics is not None:
            source_data.update(
                {
                    "initial_url": _safe_public_url(
                        diagnostics.initial_url or diagnostics.requested_url
                    ),
                    "metro_filter_url": _safe_public_url(
                        diagnostics.metro_filter_url
                    ),
                    "metro_filter_applied": diagnostics.metro_filter_applied,
                    "selected_metro_station_count": (
                        diagnostics.selected_metro_station_count
                    ),
                    "selected_metro_stations": list(
                        diagnostics.selected_metro_stations
                    ),
                    "pages_processed": diagnostics.page_count,
                    "listing_links_found": diagnostics.listing_link_count,
                    "normalized_cards": diagnostics.extracted_listing_count,
                    "rejected_cards": diagnostics.rejected_card_count,
                    "excluded_daily_rent": diagnostics.excluded_daily_rent,
                    "rejection_reasons": diagnostics.rejected_card_reasons,
                    "unique_listings": diagnostics.unique_listing_count,
                    "final_url": _safe_public_url(diagnostics.final_url),
                    "raw_metro_stations": list(diagnostics.raw_metro_stations),
                }
            )
        sources[source_name] = source_data

    payload: dict[str, object] = {
        "location_filter_mode": _location_filter_mode(config),
        "allowed_metro_stations": _configured_metro_stations(config),
        "max_metro_minutes": _max_metro_minutes(config),
        "sources": sources,
        "result_count": result_count,
    }
    if metro_statistics is not None:
        payload["filtering"] = {
            "total_unique_listings": metro_statistics.total_unique,
            "excluded_without_metro": metro_statistics.excluded_without_metro,
            "excluded_without_metro_minutes": (
                metro_statistics.excluded_without_metro_minutes
            ),
            "excluded_by_station": metro_statistics.excluded_by_station,
            "excluded_by_metro_minutes": metro_statistics.excluded_by_metro_minutes,
            "excluded_by_type": metro_statistics.excluded_by_type,
            "excluded_by_area": metro_statistics.excluded_by_area,
            "excluded_by_price": metro_statistics.excluded_by_price,
            "result_count": metro_statistics.result_count,
        }

    debug_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = debug_path.with_name(
        f".{debug_path.name}.{uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, debug_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return debug_path


def _safe_public_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    sensitive_markers = ("cookie", "authorization", "auth", "token", "session", "guid")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(marker in key.casefold() for marker in sensitive_markers)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), "")
    )


def _cian_pages_processed(
    browser_diagnostics: Mapping[str, BrowserSourceDiagnostics],
) -> int:
    diagnostics = browser_diagnostics.get("cian")
    return diagnostics.page_count if diagnostics is not None else 0


def create_saved_search_html(
    output_path: Path,
    *,
    listings: Sequence[Listing],
    config: Mapping[str, object],
    source_statuses: Sequence[SavedSourceStatus],
    warnings: Sequence[str],
    successfully_loaded_listings: bool = False,
    report_title: str = "Rental Search Report",
    created_at: datetime | None = None,
) -> Path:
    """Create one self-contained, mobile-first result page."""
    timestamp = created_at or datetime.now()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _render_saved_search_html(
            listings=listings,
            config=config,
            source_statuses=source_statuses,
            warnings=warnings,
            successfully_loaded_listings=successfully_loaded_listings,
            report_title=report_title,
            created_at=timestamp,
        ),
        encoding="utf-8",
    )
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run a saved rental search.")
    parser.add_argument(
        "--config",
        type=Path,
        default=PRESET_CONFIG_PATH,
        help="Path to a local saved-search configuration.",
    )
    parser.add_argument(
        "--source-config",
        type=Path,
        default=SOURCE_SEARCH_CONFIG_PATH,
        help="Optional source-specific browser search configuration.",
    )
    arguments = parser.parse_args(argv)
    if not arguments.config.exists():
        print(
            "Copy config/search_preset.example.json to "
            "config/search_preset.json and configure your search."
        )
        return 1

    try:
        search_run = asyncio.run(
            run_saved_search(
                config_path=arguments.config,
                cian_setup_config_path=arguments.source_config,
                console=print,
            )
        )
    except SavedSearchPresetError as error:
        print(f"Ошибка: {error}")
        return 1
    except SavedSearchCancelled:
        print("Поиск отменён. Предыдущий файл результатов сохранён.")
        return 0
    except Exception:
        print("Error: the saved search could not be prepared.")
        return 1

    print(f"Готово: {search_run.output_path.resolve()}")
    print(f"Найдено квартир: {len(search_run.listings)}")
    for source_status in search_run.source_statuses:
        print(
            f"{_source_label(source_status.source)}: "
            f"{_source_status_label(source_status.status)}"
        )

    webbrowser.open(search_run.output_path.resolve().as_uri())
    return 0


async def _search_with_adapters(
    enabled_sources: Sequence[str],
    params: SourceSearchParams,
    source_adapters: Mapping[str, SearchSource],
) -> dict[str, SourceSearchResult]:
    """Keep injected adapters for tests and non-browser programmatic callers."""
    results: dict[str, SourceSearchResult] = {}
    for source_name in enabled_sources:
        adapter = source_adapters.get(source_name)
        if adapter is None:
            continue
        try:
            results[source_name] = await adapter.search(params)
        except Exception:
            continue
    return results


def _search_params_from_config(
    config: Mapping[str, object],
    cian_calibration: Mapping[str, object] | None = None,
) -> SourceSearchParams:
    location_filter_mode = _location_filter_mode(config)
    polygon_points = (
        list(WORLD_POLYGON)
        if location_filter_mode == METRO_LOCATION_FILTER_MODE
        else _polygon_points_from_config(config)
    )

    property_types = config.get("property_types")
    if (
        not isinstance(property_types, list)
        or not property_types
        or any(item not in {"studio", "one_room"} for item in property_types)
    ):
        raise SavedSearchPresetError("Сохранённые типы жилья имеют неверный формат.")

    min_area = _optional_number(config.get("min_area"), "минимальная площадь")
    max_area = _optional_number(config.get("max_area"), "максимальная площадь")
    if min_area is not None and max_area is not None and min_area > max_area:
        raise SavedSearchPresetError("Минимальная площадь не может быть больше максимальной.")

    max_price = config.get("max_price")
    if not isinstance(max_price, (int, float)) or isinstance(max_price, bool) or max_price <= 0:
        raise SavedSearchPresetError("Максимальная цена имеет неверный формат.")

    return SourceSearchParams(
        property_types=tuple(property_types),
        min_area=min_area,
        max_area=max_area,
        max_price=int(max_price),
        polygon=tuple(polygon_points),
        metro_stations=(
            tuple(_configured_metro_stations(config))
            if location_filter_mode == METRO_LOCATION_FILTER_MODE
            else ()
        ),
        cian_search_url=(
            cian_calibration.get("search_url")
            if isinstance(cian_calibration, Mapping)
            and isinstance(cian_calibration.get("search_url"), str)
            else None
        ),
    )


def _location_filter_mode(config: Mapping[str, object]) -> str:
    mode = config.get("location_filter_mode", METRO_LOCATION_FILTER_MODE)
    if mode not in (
        METRO_LOCATION_FILTER_MODE,
        POLYGON_LOCATION_FILTER_MODE,
    ):
        raise SavedSearchPresetError("Режим фильтрации местоположения имеет неверный формат.")
    return str(mode)


def _polygon_points_from_config(
    config: Mapping[str, object],
) -> list[tuple[float, float]]:
    polygon = config.get("polygon")
    if not polygon:
        raise SavedSearchPresetError("Сначала сохраните район поиска на карте.")
    if not isinstance(polygon, dict):
        raise SavedSearchPresetError("Сохранённый район поиска имеет неверный формат.")

    geometry = polygon.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        raise SavedSearchPresetError("Сохранённый район поиска имеет неверный формат.")

    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise SavedSearchPresetError("Сохранённый район поиска имеет неверный формат.")

    ring = coordinates[0]
    if not isinstance(ring, list) or len(ring) < 4:
        raise SavedSearchPresetError("Сохранённый район поиска имеет неверный формат.")

    polygon_points: list[tuple[float, float]] = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise SavedSearchPresetError("Сохранённый район поиска имеет неверный формат.")
        try:
            polygon_points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError) as error:
            raise SavedSearchPresetError(
                "Сохранённый район поиска имеет неверный формат."
            ) from error

    if polygon_points[0] != polygon_points[-1]:
        raise SavedSearchPresetError("Сохранённый район поиска имеет неверный формат.")
    return polygon_points


def _allowed_metro_stations(config: Mapping[str, object]) -> set[str]:
    stations = _configured_metro_stations(config)

    normalized_stations = {
        normalize_metro_station(station)
        for station in stations
        if normalize_metro_station(station)
    }
    if not normalized_stations:
        raise SavedSearchPresetError("Список станций метро имеет неверный формат.")
    return normalized_stations


def _configured_metro_stations(config: Mapping[str, object]) -> list[str]:
    stations = config.get("allowed_metro_stations")
    if (
        not isinstance(stations, list)
        or not stations
        or any(not isinstance(station, str) for station in stations)
    ):
        raise SavedSearchPresetError("Список станций метро имеет неверный формат.")
    cleaned_stations = [station.strip() for station in stations if station.strip()]
    if not cleaned_stations:
        raise SavedSearchPresetError("Список станций метро имеет неверный формат.")
    return cleaned_stations


def _first_configured_station(config: Mapping[str, object]) -> str | None:
    stations = config.get("allowed_metro_stations")
    if not isinstance(stations, list):
        return None
    for station in stations:
        if isinstance(station, str) and station.strip():
            return station.strip()
    return None


def _max_metro_minutes(config: Mapping[str, object]) -> int:
    value = config.get("max_metro_minutes")
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise SavedSearchPresetError(
            "Максимальное время до метро имеет неверный формат."
        )
    return value


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise SavedSearchPresetError(f"Сохранённая настройка «{label}» имеет неверный формат.")
    return float(value)


def _enabled_real_sources(config: Mapping[str, object]) -> tuple[str, ...]:
    enabled_sources = config.get("enabled_sources")
    if not isinstance(enabled_sources, list):
        raise SavedSearchPresetError("Список источников имеет неверный формат.")

    sources = tuple(
        source for source in enabled_sources if source in REAL_SAVED_SEARCH_SOURCES
    )
    if not sources:
        raise SavedSearchPresetError("Не выбраны реальные источники для поиска.")
    return sources


def _output_relative_path(config: Mapping[str, object]) -> Path:
    output_directory = config.get("output_directory")
    if not isinstance(output_directory, str) or not output_directory.strip():
        return PRESET_OUTPUT_RELATIVE_PATH

    relative_directory = Path(output_directory)
    if relative_directory.is_absolute() or ".." in relative_directory.parts:
        raise SavedSearchPresetError("Output directory must be project-relative.")
    return relative_directory / "index.html"


def _are_duplicates(first: Listing, second: Listing) -> bool:
    if first.url and first.url == second.url:
        return True
    if first.source == second.source and first.external_id == second.external_id:
        return True
    return (
        _normalize_address(first.address) == _normalize_address(second.address)
        and round(first.area_sqm, 2) == round(second.area_sqm, 2)
        and first.rent_price == second.rent_price
    )


def _normalize_address(address: str) -> str:
    normalized = address.casefold()
    normalized = re.sub(r"\bул\.?(?=\s|,|$)", "улица", normalized)
    return re.sub(r"[^\w]+", " ", normalized).strip()


def _listing_completeness(listing: Listing) -> tuple[int, int, int, int, int]:
    has_coordinates = int(listing.latitude is not None and listing.longitude is not None)
    has_metro_station = int(bool(listing.metro_station))
    has_metro_time = int(listing.metro_minutes is not None)
    has_full_address = int(bool(listing.address) and listing.address != "Адрес не указан")
    non_empty_fields = sum(
        value not in (None, "")
        for value in (
            listing.source,
            listing.external_id,
            listing.url,
            listing.title,
            listing.property_type,
            listing.rent_price,
            listing.area_sqm,
            listing.metro_station,
            listing.metro_minutes,
            listing.address,
            listing.latitude,
            listing.longitude,
        )
    )
    return (
        has_coordinates,
        has_metro_station,
        has_metro_time,
        has_full_address,
        non_empty_fields,
    )


def _render_saved_search_html(
    *,
    listings: Sequence[Listing],
    config: Mapping[str, object],
    source_statuses: Sequence[SavedSourceStatus],
    warnings: Sequence[str],
    successfully_loaded_listings: bool,
    report_title: str,
    created_at: datetime,
) -> str:
    all_sources_failed = _all_sources_failed(source_statuses, listings)
    cards = "\n".join(_listing_card(listing) for listing in listings)
    if not cards and not all_sources_failed:
        cards = '<p class="empty">Подходящих квартир сейчас не найдено.</p>'

    result_summary = (
        """<section class="source-failure" aria-label="Не удалось получить объявления">
            <h2>Не удалось получить объявления ни из одного источника.</h2>
            <p>Поиск не смог получить объявления. Это не означает, что подходящих квартир сейчас нет. Возможная причина - временная блокировка сайтов, CAPTCHA или изменение структуры страниц.</p>
        </section>"""
        if all_sources_failed
        else f'<p class="count">Найдено: {len(listings)} {escape(_apartment_count_label(len(listings)))}</p>'
    )
    filtered_out_notice = (
        '<p class="filter-note">Объявления были получены, но не прошли заданные фильтры.</p>'
        if not listings and not all_sources_failed and successfully_loaded_listings
        else ""
    )

    warning_block = ""
    if warnings:
        warning_items = "".join(f"<li>{escape(warning)}</li>" for warning in warnings)
        warning_block = (
            '<section class="warnings" aria-label="Предупреждения источников">'
            "<h2>Обратите внимание</h2>"
            f"<ul>{warning_items}</ul>"
            "</section>"
        )

    source_labels = ", ".join(
        _source_label(source_status.source) for source_status in source_statuses
    ) or "Не указаны"
    location_filter_rows = _location_filter_rows(config)
    created_label = created_at.strftime("%d.%m.%Y %H:%M")
    count_label = _apartment_count_label(len(listings))

    return f"""<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(report_title)}</title>
    <style>
        :root {{ color-scheme: light; }}
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; color: #14211d; background: #f1f5f3; font: 18px/1.5 Arial, Helvetica, sans-serif; }}
        main {{ width: min(100%, 720px); margin: 0 auto; padding: 20px 16px 40px; }}
        h1 {{ margin: 0 0 8px; font-size: 32px; line-height: 1.15; }}
        h2 {{ margin: 0; font-size: 20px; line-height: 1.3; }}
        .summary {{ margin: 0 0 20px; color: #42544d; }}
        .count {{ margin: 0 0 20px; font-size: 22px; font-weight: 700; }}
        .filter-note {{ margin: -10px 0 20px; color: #4a5b54; font-size: 17px; }}
        .filters, .listing-card, .warnings, .source-failure {{ margin: 0 0 16px; padding: 18px; border: 1px solid #b8c6c0; border-radius: 6px; background: #ffffff; }}
        .filters ul, .warnings ul {{ margin: 10px 0 0; padding-left: 24px; }}
        .filters li, .warnings li {{ margin: 6px 0; }}
        .listing-card {{ display: grid; gap: 14px; }}
        .price {{ margin: 0; color: #0f4f43; font-size: 30px; font-weight: 700; line-height: 1.1; }}
        dl {{ display: grid; grid-template-columns: minmax(122px, 0.9fr) minmax(0, 1.5fr); gap: 8px 14px; margin: 0; }}
        dt {{ color: #4a5b54; font-weight: 700; }}
        dd {{ margin: 0; overflow-wrap: anywhere; }}
        .open-listing {{ display: flex; min-height: 58px; align-items: center; justify-content: center; padding: 12px 18px; border-radius: 6px; color: #ffffff; background: #126452; font-size: 20px; font-weight: 700; text-align: center; text-decoration: none; }}
        .open-listing:focus, .open-listing:hover {{ background: #0d4d3e; }}
        .empty {{ margin: 0; padding: 32px 18px; border: 1px solid #b8c6c0; border-radius: 6px; background: #ffffff; font-size: 21px; font-weight: 700; text-align: center; }}
        .warnings {{ border-color: #c47634; background: #fff8ed; }}
        .source-failure {{ border: 3px solid #b42318; color: #721c14; background: #fff2f0; }}
        .source-failure h2 {{ font-size: 23px; }}
        .source-failure p {{ margin: 10px 0 0; }}
        .note {{ margin: 24px 0 0; color: #4a5b54; font-size: 16px; }}
        @media (max-width: 420px) {{
            body {{ font-size: 17px; }}
            main {{ padding: 16px 12px 32px; }}
            h1 {{ font-size: 29px; }}
            dl {{ grid-template-columns: 1fr; gap: 2px; }}
            dd {{ margin-bottom: 10px; }}
        }}
    </style>
</head>
<body>
    <main>
        <h1>{escape(report_title)}</h1>
        <p class="summary">Создано: {escape(created_label)}</p>
        {result_summary}
        {filtered_out_notice}
        <section class="filters" aria-label="Сохранённые параметры поиска">
            <h2>Параметры поиска</h2>
            <ul>
                <li>Тип жилья: {escape(_property_types_label(config))}</li>
                <li>Площадь: {escape(_area_range_label(config))}</li>
                <li>Максимальная цена: {escape(_price_label(config.get('max_price')))}</li>
                {location_filter_rows}
                <li>Проверенные источники: {escape(source_labels)}</li>
            </ul>
        </section>
        <section aria-label="Найденные квартиры">
            {cards}
        </section>
        {warning_block}
        <p class="note">Объявления были актуальны на момент создания этого файла.</p>
    </main>
</body>
</html>
"""


def _listing_card(listing: Listing) -> str:
    metro_station = listing.metro_station or "Не указано"
    metro_minutes = (
        f"{listing.metro_minutes} мин"
        if listing.metro_minutes is not None
        else "Не указано"
    )
    return f"""<article class="listing-card">
    <p class="price">{escape(_price_label(listing.rent_price))}</p>
    <dl>
        <dt>Тип квартиры</dt><dd>{escape(_property_type_label(listing.property_type))}</dd>
        <dt>Площадь</dt><dd>{escape(f'{listing.area_sqm:g} м²')}</dd>
        <dt>Адрес</dt><dd>{escape(listing.address)}</dd>
        <dt>Метро</dt><dd>{escape(metro_station)}</dd>
        <dt>Время до метро</dt><dd>{escape(metro_minutes)}</dd>
        <dt>Источник</dt><dd>{escape(_source_label(listing.source))}</dd>
    </dl>
    <a class="open-listing" href="{escape(listing.url, quote=True)}">Открыть объявление</a>
</article>"""


def _all_sources_failed(
    source_statuses: Sequence[SavedSourceStatus], listings: Sequence[Listing]
) -> bool:
    return bool(source_statuses) and not listings and all(
        source_status.status != "ok" for source_status in source_statuses
    )


def _source_warning(source: str, status: str, error: str | None = None) -> str:
    label = _source_label(source)
    if error == "skipped_by_user":
        return f"{label}: источник пропущен пользователем"
    if source == "cian" and error in {
        "CIAN search is not configured. Create config/source_search.json.",
        "Saved CIAN search is no longer valid. Configure it again.",
    }:
        return error
    messages = {
        "captcha": "сайт запросил проверку CAPTCHA",
        "blocked": "сайт временно ограничил доступ",
        "parse_error": "не удалось разобрать страницу источника",
        "unavailable": "сайт временно не отдал объявления",
    }
    return f"{label}: {messages.get(status, 'не удалось получить объявления')}"


def _source_status_label(status: str) -> str:
    labels = {
        "ok": "проверен",
        "captcha": "CAPTCHA",
        "blocked": "доступ ограничен",
        "parse_error": "страница не разобрана",
        "unavailable": "временно недоступен",
    }
    return labels.get(status, "не удалось получить объявления")


def _source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def _property_types_label(config: Mapping[str, object]) -> str:
    types = config.get("property_types")
    if not isinstance(types, list):
        return "Не указано"
    return ", ".join(_property_type_label(item) for item in types)


def _property_type_label(property_type: object) -> str:
    return "Студия" if property_type == "studio" else "Однокомнатная квартира"


def _location_filter_rows(config: Mapping[str, object]) -> str:
    if _location_filter_mode(config) != METRO_LOCATION_FILTER_MODE:
        return (
            "<li>Объявления без координат: "
            f"{escape(_unverified_label(config))}</li>"
        )

    stations = config.get("allowed_metro_stations")
    station_list = (
        ", ".join(station for station in stations if isinstance(station, str))
        if isinstance(stations, list)
        else ""
    )
    first_station = (
        next(
            (
                station
                for station in stations
                if isinstance(station, str) and station.strip()
            ),
            "выбранная станция",
        )
        if isinstance(stations, list)
        else "выбранная станция"
    )
    max_metro_minutes = _max_metro_minutes(config)
    return (
        f"<li>Район: метро {escape(first_station)} и соседние станции</li>"
        f"<li>Станции: {escape(station_list)}</li>"
        f"<li>До метро: не более {max_metro_minutes} минут</li>"
    )


def _area_range_label(config: Mapping[str, object]) -> str:
    minimum = config.get("min_area")
    maximum = config.get("max_area")
    if minimum is None and maximum is None:
        return "Не указана"
    if minimum is None:
        return f"до {maximum:g} м²" if isinstance(maximum, (int, float)) else "Не указана"
    if maximum is None:
        return f"от {minimum:g} м²" if isinstance(minimum, (int, float)) else "Не указана"
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
        return f"{minimum:g}–{maximum:g} м²"
    return "Не указана"


def _price_label(price: object) -> str:
    if not isinstance(price, (int, float)):
        return "Не указана"
    return f"{price:,.0f} ₽".replace(",", " ")


def _unverified_label(config: Mapping[str, object]) -> str:
    return "Показывать" if config.get("include_unverified_locations") else "Скрывать"


def _apartment_count_label(count: int) -> str:
    remainder = count % 100
    if 11 <= remainder <= 14:
        return "квартир"
    last_digit = count % 10
    if last_digit == 1:
        return "квартира"
    if 2 <= last_digit <= 4:
        return "квартиры"
    return "квартир"


if __name__ == "__main__":
    raise SystemExit(main())
