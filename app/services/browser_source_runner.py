from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import html
import json
from pathlib import Path
import random
import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from app.models import Listing
from app.services.filtering import point_is_in_polygon
from app.sources.base import SourceSearchParams, SourceSearchResult
from app.sources.cian import CianParseError, CianSource
from app.sources.yandex_realty import YandexRealtyParseError, YandexRealtySource


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BROWSER_PROFILE_DIR = PROJECT_ROOT / "data" / "browser_profile"
DEBUG_DIR = PROJECT_ROOT / "debug"

SOURCE_LABELS = {
    "cian": "ЦИАН",
    "yandex_realty": "Яндекс Недвижимость",
}
SUPPORTED_BROWSER_SOURCES = tuple(SOURCE_LABELS)
MAX_CIAN_PAGES = 5

_CIAN_SAVED_SEARCH_PARAMETERS = (
    ("deal_type", "rent"),
    ("offer_type", "flat"),
    ("region", "1"),
    ("maxprice", None),
    ("room9", "1"),
    ("room1", "1"),
)
_CIAN_SAVED_SEARCH_PARAMETER_NAMES = frozenset(
    name for name, _value in _CIAN_SAVED_SEARCH_PARAMETERS
)

_BLOCKED_PAGE_MARKERS = (
    "access denied",
    "доступ ограничен",
    "слишком много запросов",
    "too many requests",
    "request blocked",
    "forbidden",
)
_EMPTY_RESULT_MARKERS = (
    "по вашему запросу ничего не найдено",
    "ничего не найдено",
    "нет подходящих объявлений",
)
_DOM_CARD_SELECTORS = {
    "cian": (
        "[data-name*='Card']",
        "[data-name*='Offer']",
        "article",
    ),
    "yandex_realty": (
        "[data-testid*='offer']",
        "[data-testid*='listing']",
        "[class*='OfferCard']",
        "article",
    ),
}
_LISTING_CONTENT_SELECTORS = {
    "cian": (
        "[data-name='CardComponent']",
        "[data-name*='Card']",
        "[data-name*='Offer']",
        "a[href*='/rent/flat/']",
        "article:has(a[href*='/rent/flat/'])",
    ),
    "yandex_realty": (
        "[data-testid*='offer']",
        "[data-testid*='listing']",
        "[class*='OfferCard']",
        "a[href*='/rent/flat/']",
        "article:has(a[href*='/rent/flat/'])",
    ),
}
_VISIBLE_CAPTCHA_SELECTORS = (
    "iframe[src*='captcha' i]",
    "iframe[title*='captcha' i]",
    "[data-testid*='captcha' i]",
    "[data-name*='captcha' i]",
    "[class*='captcha' i]",
    "[id*='captcha' i]",
)
_VISIBLE_CAPTCHA_TEXT_MARKERS = (
    "подтвердите, что вы не робот",
    "подтвердите, что вы человек",
    "пройдите проверку безопасности",
    "подтвердите, что запросы отправляете вы",
    "access verification",
    "security challenge",
)


@dataclass(frozen=True)
class PageRecognition:
    listing_content_visible: bool
    visible_captcha: bool
    visible_body_text: str

    @property
    def status(self) -> str:
        if self.listing_content_visible:
            return "ok"
        if self.visible_captcha:
            return "captcha"
        return "parse_error"


@dataclass
class BrowserSourceDiagnostics:
    source: str
    requested_url: str
    initial_url: str | None = None
    final_url: str | None = None
    metro_filter_url: str | None = None
    metro_filter_applied: bool = False
    selected_metro_station_count: int = 0
    selected_metro_stations: tuple[str, ...] = ()
    raw_metro_stations: tuple[str, ...] = ()
    visible_href_count: int = 0
    visible_hrefs: tuple[str, ...] = ()
    listing_link_count: int = 0
    listing_hrefs: tuple[str, ...] = ()
    candidate_card_count: int = 0
    extracted_listing_count: int = 0
    rejected_card_count: int = 0
    excluded_daily_rent: int = 0
    rejected_card_reasons: dict[str, int] = field(default_factory=dict)
    rejected_cards: list["RejectedCianCard"] = field(default_factory=list)
    cookie_banner_accepted: bool = False
    ssr_fail_detected: bool = False
    page_count: int = 0
    page_new_listing_counts: list[int] = field(default_factory=list)
    unique_listing_count: int = 0
    filtering: "CianFilteringDiagnostics | None" = None


@dataclass(frozen=True)
class CianFilteringDiagnostics:
    total: int
    excluded_by_price: int
    excluded_by_area: int
    excluded_by_type: int
    excluded_outside_polygon: int
    kept_without_coordinates: int
    result_count: int


@dataclass(frozen=True)
class RejectedCianCard:
    """Safe details for a visible CIAN card that could not be normalized."""

    url: str
    external_id: str
    reason: str
    title_found: bool
    price_found: bool
    area_found: bool
    metro_found: bool


class BrowserSearchCancelled(Exception):
    """Raised when the user cancels the whole browser-assisted search."""


class BrowserSourceRunner:
    """Acquire source results through a visible Playwright browser.

    The runner deliberately does not automate CAPTCHA interactions. It only waits
    for a person to complete a challenge in Chromium before reading rendered HTML.
    """

    def __init__(
        self,
        *,
        profile_dir: Path = BROWSER_PROFILE_DIR,
        console: Callable[[str], None] = print,
        input_func: Callable[[str], str] = input,
        playwright_factory: Callable[[], Any] | None = None,
        navigation_timeout_ms: int = 30_000,
        settle_delay_ms: int = 1_500,
        cian_page_delay_range_ms: tuple[int, int] = (2_000, 4_000),
        random_delay_ms: Callable[[int, int], int] | None = None,
        stop_command: Callable[[], str | None] | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self._console = console
        self._input_func = input_func
        self._playwright_factory = playwright_factory or _create_playwright_manager
        self._navigation_timeout_ms = navigation_timeout_ms
        self._settle_delay_ms = settle_delay_ms
        self._cian_page_delay_range_ms = cian_page_delay_range_ms
        self._random_delay_ms = random_delay_ms or random.randint
        self._stop_command = stop_command or _read_console_command
        self._sources = {
            "cian": CianSource(),
            "yandex_realty": YandexRealtySource(),
        }
        self.last_diagnostics: dict[str, BrowserSourceDiagnostics] = {}

    async def search_sources(
        self,
        source_names: Sequence[str],
        params: SourceSearchParams,
    ) -> dict[str, SourceSearchResult]:
        """Run enabled real sources in one persistent, visible browser context."""
        enabled_sources = tuple(
            source_name
            for source_name in source_names
            if source_name in SUPPORTED_BROWSER_SOURCES
        )
        results: dict[str, SourceSearchResult] = {}
        if not enabled_sources:
            return results

        context: Any | None = None
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            async with self._playwright_factory() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    headless=False,
                )
                page = await context.new_page()
                for source_name in enabled_sources:
                    results[source_name] = await self._search_source(
                        page,
                        source_name,
                        params,
                    )
        except BrowserSearchCancelled:
            raise
        except Exception:
            for source_name in enabled_sources:
                results.setdefault(
                    source_name,
                    SourceSearchResult(
                        status="unavailable",
                        total_before_filtering=0,
                        listings=[],
                        error="Не удалось открыть Chromium для получения объявлений.",
                    ),
                )
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

        return results

    async def _search_source(
        self,
        page: Any,
        source_name: str,
        params: SourceSearchParams,
    ) -> SourceSearchResult:
        source = self._sources[source_name]
        source_label = SOURCE_LABELS[source_name]
        search_url = self._build_browser_search_url(source_name, params)
        if source_name == "cian" and params.metro_stations:
            if not params.cian_search_url:
                diagnostics = BrowserSourceDiagnostics(
                    source=source_name,
                    requested_url="",
                )
                self.last_diagnostics[source_name] = diagnostics
                return SourceSearchResult(
                    status="parse_error",
                    total_before_filtering=0,
                    listings=[],
                    error=(
                        "CIAN search is not configured. Create config/source_search.json."
                    ),
                )
            search_url = _cian_saved_search_url(
                params.cian_search_url,
                max_price=params.max_price,
            )
        diagnostics = BrowserSourceDiagnostics(
            source=source_name,
            requested_url=search_url,
        )
        self.last_diagnostics[source_name] = diagnostics
        if source_name == "cian":
            return await self._search_cian_pages(
                page,
                source,
                search_url,
                params,
                diagnostics,
            )

        self._console(f"Открываю {source_label}...")
        self._console("Ожидаю загрузку страницы...")

        try:
            response = await page.goto(
                search_url,
                wait_until="domcontentloaded",
                timeout=self._navigation_timeout_ms,
            )
            if self._response_status(response) in {403, 429}:
                return SourceSearchResult(
                    status="blocked",
                    total_before_filtering=0,
                    listings=[],
                    error=f"{source_label} временно ограничил доступ.",
                )
            if self._response_status(response) is not None and self._response_status(response) >= 400:
                return SourceSearchResult(
                    status="unavailable",
                    total_before_filtering=0,
                    listings=[],
                    error=f"{source_label} временно недоступен.",
                )
            diagnostics.cookie_banner_accepted = await self._accept_cookie_banner(page)
            await self._wait_for_rendered_page(page, source_name)
            page_html = await page.content()
            diagnostics.final_url = page.url
            diagnostics.ssr_fail_detected = "_ssr_fail" in page.url
        except Exception:
            return SourceSearchResult(
                status="unavailable",
                total_before_filtering=0,
                listings=[],
                error=f"Не удалось открыть страницу {source_label}.",
            )

        recognition = await self._recognize_page(page, source_name)
        if recognition.status == "ok":
            self._console(
                f"{source_label}: найдена обычная выдача, CAPTCHA отсутствует."
            )
            return await self._parse_rendered_page(
                page, source_name, source, page_html
            )
        if recognition.status == "captcha":
            self._console(f"{source_label}: видимая CAPTCHA обнаружена.")
            return await self._wait_for_manual_verification(page, source_name, source)

        if self._looks_like_blocked_page(recognition.visible_body_text):
            return SourceSearchResult(
                status="blocked",
                total_before_filtering=0,
                listings=[],
                error=f"{source_label} временно ограничил доступ.",
            )

        self._console(f"{source_label}: ни выдача, ни CAPTCHA не распознаны.")
        return self._unrecognized_page_result(source_label)

    async def _search_cian_pages(
        self,
        page: Any,
        source: CianSource,
        search_url: str,
        params: SourceSearchParams,
        diagnostics: BrowserSourceDiagnostics,
    ) -> SourceSearchResult:
        """Read a small, sequential window of the rendered CIAN result pages."""
        source_name = "cian"
        source_label = SOURCE_LABELS[source_name]
        current_url = search_url
        requested_metro_stations = tuple(params.metro_stations)
        listings: list[Listing] = []
        seen_external_ids: set[str] = set()
        diagnostics.initial_url = _safe_public_url(search_url)

        if requested_metro_stations:
            self._console(
                f"ЦИАН: итоговый URL: {diagnostics.initial_url or search_url}"
            )
            if not _is_valid_cian_saved_search_url(
                diagnostics.initial_url,
                max_price=params.max_price,
            ):
                return _invalid_cian_saved_search_url_result()

        for page_number in range(1, MAX_CIAN_PAGES + 1):
            if page_number == 1:
                self._console(f"Открываю {source_label}...")
            else:
                self._console(f"{source_label}: открываю страницу {page_number}...")
            self._console("Ожидаю загрузку страницы...")

            page_diagnostics = BrowserSourceDiagnostics(
                source=source_name,
                requested_url=current_url,
            )
            try:
                response = await page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=self._navigation_timeout_ms,
                )
                response_status = self._response_status(response)
                if response_status in {403, 429}:
                    if page_number == 1 and requested_metro_stations:
                        return _saved_cian_search_invalid_result()
                    return self._cian_page_error_or_partial_result(
                        listings,
                        diagnostics,
                        params,
                        status="blocked",
                        error=f"{source_label} временно ограничил доступ.",
                    )
                if response_status is not None and response_status >= 400:
                    if page_number == 1 and requested_metro_stations:
                        return _saved_cian_search_invalid_result()
                    return self._cian_page_error_or_partial_result(
                        listings,
                        diagnostics,
                        params,
                        status="unavailable",
                        error=f"{source_label} временно недоступен.",
                    )

                page_diagnostics.cookie_banner_accepted = (
                    await self._accept_cookie_banner(page)
                )
                await self._wait_for_rendered_page(page, source_name)
                page_html = await page.content()
                page_diagnostics.final_url = page.url
            except Exception:
                if page_number == 1 and requested_metro_stations:
                    return _saved_cian_search_invalid_result()
                return self._cian_page_error_or_partial_result(
                    listings,
                    diagnostics,
                    params,
                    status="unavailable",
                    error=f"Не удалось открыть страницу {source_label}.",
                )

            if page_number == 1 and requested_metro_stations:
                saved_url = _safe_public_url(page.url)
                if not _is_valid_cian_saved_search_url(
                    saved_url,
                    max_price=params.max_price,
                ):
                    return _saved_cian_search_invalid_result()
                diagnostics.metro_filter_url = saved_url
                diagnostics.metro_filter_applied = True
                diagnostics.selected_metro_station_count = len(requested_metro_stations)
                diagnostics.selected_metro_stations = requested_metro_stations
                self._console(
                    f"ЦИАН: исходный URL: {diagnostics.initial_url or search_url}"
                )
                self._console(
                    f"ЦИАН: итоговый URL после настройки: {saved_url}"
                )
                self._console(
                    "ЦИАН: станций подтверждено в сохранённом поиске: "
                    f"{len(requested_metro_stations)}."
                )

            recognition = await self._recognize_page(page, source_name)
            if (
                page_number == 1
                and requested_metro_stations
                and await _has_visible_captcha(page, recognition.visible_body_text)
            ):
                self._console(f"{source_label}: видимая CAPTCHA обнаружена.")
                return _saved_cian_search_invalid_result()
            if recognition.status == "captcha":
                self._console(f"{source_label}: видимая CAPTCHA обнаружена.")
                if page_number == 1:
                    if requested_metro_stations:
                        return _saved_cian_search_invalid_result()
                    manual_result = await self._wait_for_manual_verification(
                        page,
                        source_name,
                        source,
                        return_page_ready=True,
                    )
                    if manual_result is not None:
                        return manual_result
                    await self._wait_for_rendered_page(page, source_name)
                    page_html = await page.content()
                    page_diagnostics.final_url = page.url
                    recognition = await self._recognize_page(page, source_name)
                    if recognition.status == "captcha":
                        return SourceSearchResult(
                            status="captcha",
                            total_before_filtering=0,
                            listings=[],
                            error="ЦИАН запросил CAPTCHA.",
                        )
                else:
                    self._console(
                        f"{source_label}: CAPTCHA появилась, дальнейшие страницы "
                        "не обрабатываются."
                    )
                    break
            if recognition.status != "ok":
                if page_number == 1 and requested_metro_stations:
                    return _saved_cian_search_invalid_result()
                if self._looks_like_blocked_page(recognition.visible_body_text):
                    return self._cian_page_error_or_partial_result(
                        listings,
                        diagnostics,
                        params,
                        status="blocked",
                        error=f"{source_label} временно ограничил доступ.",
                    )
                if listings:
                    self._console(
                        f"{source_label}: страница {page_number} не распознана, "
                        "дальнейшие страницы не обрабатываются."
                    )
                    break
                self._console(
                    f"{source_label}: ни выдача, ни CAPTCHA не распознаны."
                )
                return self._unrecognized_page_result(source_label)

            self._console(
                f"{source_label}: найдена обычная выдача, CAPTCHA отсутствует."
            )
            page_result = await self._parse_rendered_page(
                page,
                source_name,
                source,
                page_html,
                diagnostics=page_diagnostics,
            )
            self._merge_cian_page_diagnostics(diagnostics, page_diagnostics)
            diagnostics.page_count = page_number
            if page_result.status != "ok":
                diagnostics.page_new_listing_counts.append(0)
                if page_diagnostics.listing_link_count:
                    self._console(
                        f"{source_label}: страница {page_number} содержит ссылки, "
                        "но карточки не удалось нормализовать. Продолжаю обход."
                    )
                elif listings:
                    self._console(
                        f"{source_label}: страница {page_number} не разобрана, "
                        "дальнейшие страницы не обрабатываются."
                    )
                    break
                else:
                    return page_result
            else:
                new_listings = [
                    listing
                    for listing in page_result.listings
                    if listing.external_id not in seen_external_ids
                ]
                seen_external_ids.update(
                    listing.external_id for listing in page_result.listings
                )
                listings.extend(new_listings)
                diagnostics.page_new_listing_counts.append(len(new_listings))
                if page_number == 1:
                    self._console(
                        f"{source_label}: страница 1 - извлечено "
                        f"{len(new_listings)} объявлений."
                    )
                else:
                    self._console(
                        f"{source_label}: страница {page_number} - извлечено "
                        f"{len(new_listings)} новых объявлений."
                    )

                if page_result.listings and not new_listings:
                    self._console(
                        f"{source_label}: страница {page_number} повторяет уже "
                        "полученные объявления, обход остановлен."
                    )
                    break

            next_url = await _next_cian_page_url(page)
            if next_url is None:
                self._console(
                    f"{source_label}: следующая страница не найдена, обход завершён."
                )
                break
            if requested_metro_stations:
                next_url = _cian_saved_search_url(
                    next_url,
                    max_price=params.max_price,
                )
            if page_number == MAX_CIAN_PAGES:
                self._console(
                    f"{source_label}: достигнут лимит {MAX_CIAN_PAGES} страниц."
                )
                break

            command = (self._stop_command() or "").strip().casefold()
            if command == "q":
                raise BrowserSearchCancelled()
            if command == "s":
                self._console(
                    f"{source_label}: обход следующих страниц остановлен пользователем."
                )
                break

            minimum_delay, maximum_delay = self._cian_page_delay_range_ms
            delay_ms = self._random_delay_ms(minimum_delay, maximum_delay)
            await page.wait_for_timeout(delay_ms)
            current_url = next_url

        return self._finalize_cian_result(listings, diagnostics, params)

    def _cian_page_error_or_partial_result(
        self,
        listings: Sequence[Listing],
        diagnostics: BrowserSourceDiagnostics,
        params: SourceSearchParams,
        *,
        status: str,
        error: str,
    ) -> SourceSearchResult:
        if listings:
            self._console(f"ЦИАН: {error} Новые страницы не обрабатываются.")
            return self._finalize_cian_result(listings, diagnostics, params)
        return SourceSearchResult(
            status=status,
            total_before_filtering=0,
            listings=[],
            error=error,
        )

    def _finalize_cian_result(
        self,
        listings: Sequence[Listing],
        diagnostics: BrowserSourceDiagnostics,
        params: SourceSearchParams,
    ) -> SourceSearchResult:
        diagnostics.unique_listing_count = len(listings)
        diagnostics.extracted_listing_count = len(listings)
        _write_cian_rejected_cards(diagnostics.rejected_cards)
        diagnostics.raw_metro_stations = tuple(
            sorted(
                {
                    listing.metro_station
                    for listing in listings
                    if listing.metro_station
                }
            )
        )
        diagnostics.filtering = _cian_filtering_diagnostics(listings, params)
        self._console(
            f"ЦИАН: всего уникальных объявлений: {diagnostics.unique_listing_count}."
        )
        self._console(
            "ЦИАН: станции в сырых карточках: "
            f"{', '.join(diagnostics.raw_metro_stations) or 'не указаны'}."
        )
        self._log_cian_filtering_diagnostics(diagnostics.filtering)
        self._log_listing_samples("ЦИАН", listings)
        if diagnostics.listing_link_count and not listings:
            return SourceSearchResult(
                status="parse_error",
                total_before_filtering=0,
                listings=[],
                error="ЦИАН: выдача открылась, но карточки не удалось извлечь.",
            )
        return SourceSearchResult(
            status="ok",
            total_before_filtering=len(listings),
            listings=list(listings),
        )

    def _log_cian_filtering_diagnostics(
        self,
        diagnostics: CianFilteringDiagnostics,
    ) -> None:
        self._console(f"ЦИАН: исключено по цене: {diagnostics.excluded_by_price}.")
        self._console(
            f"ЦИАН: исключено по площади: {diagnostics.excluded_by_area}."
        )
        self._console(f"ЦИАН: исключено по типу: {diagnostics.excluded_by_type}.")
        self._console(
            "ЦИАН: исключено вне выбранного полигона: "
            f"{diagnostics.excluded_outside_polygon}."
        )
        self._console(
            "ЦИАН: оставлено без координат: "
            f"{diagnostics.kept_without_coordinates}."
        )
        self._console(
            "ЦИАН: итоговое количество после локальной фильтрации: "
            f"{diagnostics.result_count}."
        )

    @staticmethod
    def _merge_cian_page_diagnostics(
        diagnostics: BrowserSourceDiagnostics,
        page_diagnostics: BrowserSourceDiagnostics,
    ) -> None:
        diagnostics.final_url = page_diagnostics.final_url
        diagnostics.visible_href_count += page_diagnostics.visible_href_count
        diagnostics.visible_hrefs = tuple(
            dict.fromkeys(
                (*diagnostics.visible_hrefs, *page_diagnostics.visible_hrefs)
            )
        )
        diagnostics.listing_link_count += page_diagnostics.listing_link_count
        diagnostics.listing_hrefs = tuple(
            dict.fromkeys(
                (*diagnostics.listing_hrefs, *page_diagnostics.listing_hrefs)
            )
        )
        diagnostics.candidate_card_count += page_diagnostics.candidate_card_count
        diagnostics.rejected_card_count += page_diagnostics.rejected_card_count
        diagnostics.excluded_daily_rent += page_diagnostics.excluded_daily_rent
        diagnostics.rejected_cards.extend(page_diagnostics.rejected_cards)
        for reason, count in page_diagnostics.rejected_card_reasons.items():
            diagnostics.rejected_card_reasons[reason] = (
                diagnostics.rejected_card_reasons.get(reason, 0) + count
            )
        diagnostics.cookie_banner_accepted = (
            diagnostics.cookie_banner_accepted
            or page_diagnostics.cookie_banner_accepted
        )

    async def _wait_for_manual_verification(
        self,
        page: Any,
        source_name: str,
        source: Any,
        *,
        return_page_ready: bool = False,
    ) -> SourceSearchResult | None:
        source_label = SOURCE_LABELS[source_name]
        self._console("Найдена CAPTCHA. Решите её в браузере.")

        while True:
            try:
                choice = await asyncio.to_thread(
                    self._input_func,
                    "Сайт запросил проверку. Решите CAPTCHA в открытом браузере, "
                    "дождитесь загрузки объявлений и затем нажмите Enter в этом окне.\n"
                    "Enter — продолжить, S — пропустить источник, Q — отменить поиск: ",
                )
            except (EOFError, KeyboardInterrupt) as error:
                raise BrowserSearchCancelled() from error

            normalized_choice = choice.strip().casefold()
            if normalized_choice == "q":
                raise BrowserSearchCancelled()
            if normalized_choice == "s":
                return SourceSearchResult(
                    status="unavailable",
                    total_before_filtering=0,
                    listings=[],
                    error="skipped_by_user",
                )

            try:
                await page.wait_for_timeout(self._settle_delay_ms)
                page_html = await page.content()
            except Exception:
                return SourceSearchResult(
                    status="unavailable",
                    total_before_filtering=0,
                    listings=[],
                    error=f"Не удалось прочитать страницу {source_label} после проверки.",
                )

            recognition = await self._recognize_page(page, source_name)
            if recognition.status == "ok":
                self._console(
                    f"{source_label}: найдена обычная выдача, CAPTCHA отсутствует."
                )
                if return_page_ready:
                    return None
                return await self._parse_rendered_page(
                    page, source_name, source, page_html
                )
            if recognition.status == "captcha":
                self._console(
                    f"{source_label}: видимая CAPTCHA обнаружена. Проверка всё ещё "
                    "отображается. Завершите её в браузере или пропустите источник."
                )
                continue
            if self._looks_like_blocked_page(recognition.visible_body_text):
                return SourceSearchResult(
                    status="blocked",
                    total_before_filtering=0,
                    listings=[],
                    error=f"{source_label} временно ограничил доступ.",
                )
            self._console(f"{source_label}: ни выдача, ни CAPTCHA не распознаны.")
            return self._unrecognized_page_result(source_label)

    async def _parse_rendered_page(
        self,
        page: Any,
        source_name: str,
        source: Any,
        page_html: str,
        *,
        diagnostics: BrowserSourceDiagnostics | None = None,
    ) -> SourceSearchResult:
        source_label = SOURCE_LABELS[source_name]
        self._console("Получаю объявления...")

        diagnostics = diagnostics or self.last_diagnostics.get(source_name)
        if diagnostics is None:
            diagnostics = BrowserSourceDiagnostics(source_name, page.url)
            self.last_diagnostics[source_name] = diagnostics

        if source_name == "cian":
            listings = await self._extract_cian_browser_listings(
                page,
                diagnostics,
                page_html,
            )
        else:
            listings = await self._extract_yandex_browser_listings(page, diagnostics)

        if diagnostics.listing_link_count:
            diagnostics.extracted_listing_count = len(listings)
            if not listings:
                return SourceSearchResult(
                    status="parse_error",
                    total_before_filtering=0,
                    listings=[],
                    error=(
                        f"{source_label}: выдача открылась, но карточки не удалось "
                        "извлечь."
                    ),
                )
            self._console(f"{source_label}: получено {len(listings)} объявлений.")
            self._log_listing_samples(source_label, listings)
            return SourceSearchResult(
                status="ok",
                total_before_filtering=len(listings),
                listings=listings,
            )

        if self._looks_like_empty_results(
            source,
            await _visible_body_text(page),
        ):
            self._console(f"{source_label}: получено 0 объявлений.")
            return SourceSearchResult(
                status="ok",
                total_before_filtering=0,
                listings=[],
            )

        # Existing source parsers remain a fallback for pages without rendered
        # listing links, for example a server-confirmed empty result page.
        try:
            listings = source.parse_listings(page_html)
        except (CianParseError, YandexRealtyParseError):
            listings = await self._extract_listings_from_dom(page, source_name)

        if not listings:
            return SourceSearchResult(
                status="parse_error",
                total_before_filtering=0,
                listings=[],
                error=(
                    f"После загрузки {source_label} карточки объявлений не найдены "
                    "ни в HTML, ни в отображаемой странице."
                ),
            )

        self._console(f"{source_label}: получено {len(listings)} объявлений.")
        self._log_listing_samples(source_label, listings)
        return SourceSearchResult(
            status="ok",
            total_before_filtering=len(listings),
            listings=listings,
        )

    async def _extract_cian_browser_listings(
        self,
        page: Any,
        diagnostics: BrowserSourceDiagnostics,
        page_html: str,
    ) -> list[Listing]:
        return await self._extract_browser_dom_listings(
            page,
            "cian",
            diagnostics,
            coordinate_hints=_cian_coordinates_from_page_data(page_html),
        )

    def _log_listing_samples(
        self,
        source_label: str,
        listings: Sequence[Listing],
    ) -> None:
        for listing in listings[:3]:
            self._console(
                f"{source_label}: пример Listing: "
                f"{listing.model_dump(mode='json')}"
            )

    async def _extract_yandex_browser_listings(
        self,
        page: Any,
        diagnostics: BrowserSourceDiagnostics,
    ) -> list[Listing]:
        return await self._extract_browser_dom_listings(
            page,
            "yandex_realty",
            diagnostics,
        )

    async def _extract_browser_dom_listings(
        self,
        page: Any,
        source_name: str,
        diagnostics: BrowserSourceDiagnostics,
        coordinate_hints: dict[str, tuple[float, float]] | None = None,
    ) -> list[Listing]:
        """Normalize visible source cards through their public listing links."""
        source_label = SOURCE_LABELS[source_name]
        visible_links = await _visible_page_links(page)
        diagnostics.visible_href_count = len(visible_links)
        diagnostics.visible_hrefs = tuple(url for _, url in visible_links)
        listing_links = [
            (
                link,
                (_safe_public_url(url) or url) if source_name == "cian" else url,
            )
            for link, url in visible_links
            if _is_source_listing_url(source_name, url)
        ]
        diagnostics.listing_link_count = len(listing_links)
        diagnostics.listing_hrefs = tuple(url for _, url in listing_links)
        self._console(
            f"{source_label}: всего видимых href: {diagnostics.visible_href_count}."
        )
        self._console(
            f"{source_label}: первые видимые href: "
            f"{', '.join(diagnostics.visible_hrefs[:10]) or 'нет'}"
        )
        self._console(
            f"{source_label}: найдено ссылок на объявления: "
            f"{diagnostics.listing_link_count}."
        )
        self._console(
            f"{source_label}: первые ссылки: "
            f"{', '.join(diagnostics.listing_hrefs[:10]) or 'нет'}"
        )

        cards: list[tuple[Any, str, Any]] = []
        for link, listing_url in listing_links:
            card = await _card_container_for_link(link)
            if card is not None:
                cards.append((card, listing_url, link))
            elif source_name == "cian":
                _record_rejected_cian_card(
                    diagnostics,
                    RejectedCianCard(
                        url=listing_url,
                        external_id=_external_id_from_url(listing_url),
                        reason="card_container_not_found",
                        title_found=False,
                        price_found=False,
                        area_found=False,
                        metro_found=False,
                    ),
                )

        fallback_cards = await _visible_card_containers(page, source_name)
        diagnostics.candidate_card_count = max(len(cards), len(fallback_cards))
        self._console(
            f"{source_label}: потенциальных контейнеров карточек: "
            f"{diagnostics.candidate_card_count}."
        )
        if cards:
            await self._log_first_card_text(source_name, cards[0][0])
        elif fallback_cards:
            await self._log_first_card_text(source_name, fallback_cards[0])

        listings: list[Listing] = []
        seen_urls: set[str] = set()
        for card, listing_url, link in cards:
            try:
                listing, rejected_card = await self._listing_from_browser_card(
                    card,
                    link,
                    source_name,
                    listing_url,
                    coordinate_hints=coordinate_hints,
                )
            except Exception:
                if source_name == "cian":
                    _record_rejected_cian_card(
                        diagnostics,
                        RejectedCianCard(
                            url=listing_url,
                            external_id=_external_id_from_url(listing_url),
                            reason="normalization_error",
                            title_found=False,
                            price_found=False,
                            area_found=False,
                            metro_found=False,
                        ),
                    )
                continue
            if rejected_card is not None:
                _record_rejected_cian_card(diagnostics, rejected_card)
            if listing is None or listing.url in seen_urls:
                continue
            seen_urls.add(listing.url)
            listings.append(listing)

        return listings

    async def _log_first_card_text(self, source_name: str, card: Any) -> None:
        source_label = SOURCE_LABELS[source_name]
        try:
            card_text = _compact_text(await card.inner_text())
            self._console(
                f"{source_label}: текст первой карточки: {card_text[:600]}"
            )
        except Exception:
            return

    async def _listing_from_browser_card(
        self,
        card: Any,
        link: Any,
        source_name: str,
        listing_url: str,
        *,
        coordinate_hints: dict[str, tuple[float, float]] | None = None,
    ) -> tuple[Listing | None, RejectedCianCard | None]:
        raw_text = await card.inner_text()
        text = _compact_text(raw_text)
        link_title = _compact_text(await link.inner_text())
        title = await _first_visible_text(
            card,
            (
                "[itemprop='name']",
                "[data-mark='OfferTitle']",
                "[data-testid*='title' i]",
                "[data-name*='title' i]",
                "h1, h2, h3",
            ),
        )
        property_type = (
            _property_type_from_text(title or "")
            or _property_type_from_text(link_title)
            or _property_type_from_text(text)
        )
        price_text = await _first_visible_text(
            card,
            (
                "[data-mark='MainPrice']",
                "[itemprop='price']",
                "[data-testid*='main-price' i]",
                "[data-name*='main-price' i]",
            ),
        )
        if source_name == "cian":
            price = _rental_price_from_card(
                main_price_text=price_text,
                card_text=text,
            )
            daily_rent_detected = _has_daily_rent_marker(
                " ".join(value for value in (price_text, text) if value)
            )
        else:
            price = _price_from_text(price_text or text)
            daily_rent_detected = False
        area = _area_from_text(title or link_title) or _area_from_text(text)

        metro_text = await _first_visible_text(
            card,
            (
                "[data-name='SpecialGeo']",
                "[data-testid*='metro' i]",
                "[aria-label*='метро' i]",
            ),
        )
        metro_station, metro_minutes = _metro_from_text(metro_text or text)

        external_id = _external_id_from_url(listing_url)
        if property_type is None or price is None or area is None:
            missing_parts = []
            if property_type is None:
                missing_parts.append("property_type_not_found")
            if price is None:
                missing_parts.append(
                    "daily_rent" if daily_rent_detected else "price_not_found"
                )
            if area is None:
                missing_parts.append("area_not_found")
            return None, RejectedCianCard(
                url=listing_url,
                external_id=external_id,
                reason=", ".join(missing_parts),
                title_found=bool(title or link_title),
                price_found=price is not None,
                area_found=area is not None,
                metro_found=metro_station is not None,
            )

        latitude, longitude = await _coordinates_from_card(card)
        if (
            (latitude is None or longitude is None)
            and coordinate_hints is not None
        ):
            latitude, longitude = coordinate_hints.get(
                external_id,
                (None, None),
            )
        return (
            Listing(
                source=source_name,
                external_id=external_id,
                url=listing_url,
                title=title or link_title or _title_from_text(text),
                property_type=property_type,
                rent_price=price,
                area_sqm=area,
                metro_station=metro_station,
                metro_minutes=metro_minutes,
                address=_address_from_card_text(raw_text),
                latitude=latitude,
                longitude=longitude,
                location_verified=latitude is not None and longitude is not None,
            ),
            None,
        )

    async def _extract_listings_from_dom(
        self, page: Any, source_name: str
    ) -> list[Listing]:
        """Fallback for rendered cards when a source's embedded JSON changed."""
        listings: list[Listing] = []
        seen_urls: set[str] = set()

        for selector in _DOM_CARD_SELECTORS[source_name]:
            try:
                cards = page.locator(selector)
                card_count = min(await cards.count(), 50)
            except Exception:
                continue

            for index in range(card_count):
                try:
                    listing = await self._listing_from_dom_card(
                        cards.nth(index),
                        source_name,
                        page.url,
                    )
                except Exception:
                    continue

                if listing is None or listing.url in seen_urls:
                    continue
                seen_urls.add(listing.url)
                listings.append(listing)

            if listings:
                break

        return listings

    async def _listing_from_dom_card(
        self,
        card: Any,
        source_name: str,
        page_url: str,
    ) -> Listing | None:
        text = _compact_text(await card.inner_text())
        property_type = _property_type_from_text(text)
        price = _price_from_text(text)
        area = _area_from_text(text)
        if property_type is None or price is None or area is None:
            return None

        link = card.locator("a[href]").first
        href = await link.get_attribute("href")
        if not href:
            return None
        url = urljoin(page_url, href)

        latitude, longitude = await _coordinates_from_card(card)
        metro_station, metro_minutes = _metro_from_text(text)
        return Listing(
            source=source_name,
            external_id=_external_id_from_url(url),
            url=url,
            title=_title_from_text(text),
            property_type=property_type,
            rent_price=price,
            area_sqm=area,
            metro_station=metro_station,
            metro_minutes=metro_minutes,
            address=_address_from_text(text),
            latitude=latitude,
            longitude=longitude,
            location_verified=latitude is not None and longitude is not None,
        )

    @staticmethod
    def _response_status(response: Any) -> int | None:
        status = getattr(response, "status", None)
        return status if isinstance(status, int) else None

    def _build_browser_search_url(
        self,
        source_name: str,
        params: SourceSearchParams,
    ) -> str:
        if source_name == "cian":
            return self._sources[source_name].build_search_url(params)

        # Legacy query parameters can open an SSR error page. The stable public
        # Moscow rental catalogue is intentionally kept minimal; saved filters are
        # still applied locally after DOM extraction.
        return YandexRealtySource.base_url

    async def _wait_for_rendered_page(self, page: Any, source_name: str) -> None:
        wait_for_load_state = getattr(page, "wait_for_load_state", None)
        if callable(wait_for_load_state):
            try:
                await wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
        await page.wait_for_timeout(self._settle_delay_ms)

        selectors = {
            "cian": (
                "a[href*='/rent/flat/']",
                "[data-testid*='card' i]",
                "[data-name*='Card' i]",
            ),
            "yandex_realty": (
                "a[href*='/offer/']",
                "[data-testid*='offer' i]",
                "[data-testid*='card' i]",
            ),
        }[source_name]
        for _ in range(6):
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    if await locator.count() and await locator.nth(0).is_visible():
                        return
                except Exception:
                    continue
            await page.wait_for_timeout(1_000)

    async def _accept_cookie_banner(self, page: Any) -> bool:
        for selector in (
            "button:has-text('Принять')",
            "button:has-text('Согласен')",
            "button:has-text('Разрешить все')",
        ):
            try:
                button = page.locator(selector)
                if await button.count() and await button.nth(0).is_visible():
                    await button.nth(0).click(timeout=3_000)
                    return True
            except Exception:
                continue
        return False

    async def _recognize_page(
        self, page: Any, source_name: str
    ) -> PageRecognition:
        """Classify only rendered, visible page content, never script markup."""
        visible_body_text = await _visible_body_text(page)
        listing_content_visible = await _has_visible_listing_content(
            page,
            source_name,
            visible_body_text,
        )
        visible_captcha = False
        if not listing_content_visible:
            visible_captcha = await _has_visible_captcha(page, visible_body_text)
        return PageRecognition(
            listing_content_visible=listing_content_visible,
            visible_captcha=visible_captcha,
            visible_body_text=visible_body_text,
        )

    @staticmethod
    def _looks_like_blocked_page(visible_body_text: str) -> bool:
        normalized_html = visible_body_text.casefold()
        return any(
            marker in normalized_html for marker in _BLOCKED_PAGE_MARKERS
        )

    @staticmethod
    def _looks_like_empty_results(source: Any, page_html: str) -> bool:
        source_check = getattr(source, "_looks_like_empty_results", None)
        if callable(source_check) and source_check(page_html):
            return True
        normalized_html = page_html.casefold()
        return any(marker in normalized_html for marker in _EMPTY_RESULT_MARKERS)

    @staticmethod
    def _unrecognized_page_result(source_label: str) -> SourceSearchResult:
        return SourceSearchResult(
            status="parse_error",
            total_before_filtering=0,
            listings=[],
            error=(
                f"На видимой странице {source_label} не распознаны ни карточки "
                "объявлений, ни активная CAPTCHA."
            ),
        )


def _create_playwright_manager() -> Any:
    """Import Playwright only when the desktop workflow is actually started."""
    from playwright.async_api import async_playwright

    return async_playwright()


def _read_console_command() -> str | None:
    """Read one already-pressed Windows console key without blocking."""
    try:
        import msvcrt
    except ImportError:
        return None

    if not msvcrt.kbhit():
        return None
    return msvcrt.getwch()


def _cian_url_has_metro_filter(url: str) -> bool:
    return any(
        key.casefold().startswith("metro")
        for key in parse_qs(urlsplit(url).query, keep_blank_values=True)
    )


def _cian_saved_search_url(search_url: str, *, max_price: int) -> str:
    """Build a public CIAN URL without changing the saved metro calibration."""
    safe_url = _safe_public_url(search_url)
    if safe_url is None:
        return search_url

    parsed = urlsplit(safe_url)
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if name.casefold() not in _CIAN_SAVED_SEARCH_PARAMETER_NAMES
    ]
    required_parameters = [
        (name, str(max_price) if value is None else value)
        for name, value in _CIAN_SAVED_SEARCH_PARAMETERS
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode([*query, *required_parameters], doseq=True),
            "",
        )
    )


def _is_valid_cian_saved_search_url(
    url: str | None,
    *,
    max_price: int,
) -> bool:
    if url is None or not _cian_url_has_metro_filter(url):
        return False

    values_by_name: dict[str, list[str]] = {}
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        values_by_name.setdefault(name.casefold(), []).append(value)

    expected_values = {
        "deal_type": "rent",
        "offer_type": "flat",
        "region": "1",
        "maxprice": str(max_price),
        "room9": "1",
        "room1": "1",
    }
    return all(values_by_name.get(name) == [value] for name, value in expected_values.items())


def _invalid_cian_saved_search_url_result() -> SourceSearchResult:
    return SourceSearchResult(
        status="parse_error",
        total_before_filtering=0,
        listings=[],
        error="ЦИАН: итоговый URL не содержит обязательные параметры поиска.",
    )


def _saved_cian_search_invalid_result() -> SourceSearchResult:
    return SourceSearchResult(
        status="parse_error",
        total_before_filtering=0,
        listings=[],
        error=(
            "Сохранённый поиск ЦИАНа больше не действует. "
            "Запустите настройку заново."
        ),
    )


def _safe_public_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    sensitive_markers = ("cookie", "authorization", "auth", "token", "session", "guid")
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(marker in key.casefold() for marker in sensitive_markers)
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(safe_query, doseq=True),
            "",
        )
    )


def _record_rejected_cian_card(
    diagnostics: BrowserSourceDiagnostics,
    rejected_card: RejectedCianCard,
) -> None:
    diagnostics.rejected_cards.append(rejected_card)
    diagnostics.rejected_card_count += 1
    if "daily_rent" in rejected_card.reason.split(", "):
        diagnostics.excluded_daily_rent += 1
    for reason in rejected_card.reason.split(", "):
        diagnostics.rejected_card_reasons[reason] = (
            diagnostics.rejected_card_reasons.get(reason, 0) + 1
        )


def _write_cian_rejected_cards(rejected_cards: Sequence[RejectedCianCard]) -> Path:
    """Persist compact parser diagnostics without card HTML or browser state."""
    debug_path = DEBUG_DIR / "cian_rejected_cards.json"
    payload = [
        {
            "url": _safe_public_url(card.url) or card.url,
            "external_id": card.external_id,
            "reason": card.reason,
            "title_found": card.title_found,
            "price_found": card.price_found,
            "area_found": card.area_found,
            "metro_found": card.metro_found,
        }
        for card in rejected_cards
    ]
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return debug_path


async def _next_cian_page_url(page: Any) -> str | None:
    current_url = page.url
    candidates: list[str] = []

    for selector in (
        "a[aria-label*='Следующая' i]",
        "a[title*='Следующая' i]",
        "[data-testid*='pagination' i] a[href]",
        "[data-name*='Pagination' i] a[href]",
    ):
        try:
            links = page.locator(selector)
            count = min(await links.count(), 20)
        except Exception:
            continue

        for index in range(count):
            try:
                link = links.nth(index)
                if not await link.is_visible():
                    continue
                href = await link.get_attribute("href")
            except Exception:
                continue
            if href:
                candidates.append(urljoin(current_url, href))

    candidates.extend(url for _, url in await _visible_page_links(page))
    return _nearest_next_cian_page_url(current_url, candidates)


def _nearest_next_cian_page_url(
    current_url: str,
    candidates: Sequence[str],
) -> str | None:
    current_page = _cian_page_number(current_url)
    pages: list[tuple[int, str]] = []
    for candidate in candidates:
        parsed_url = urlsplit(candidate)
        if (
            "cian.ru" not in (parsed_url.hostname or "").casefold()
            or parsed_url.path.casefold() != "/cat.php"
        ):
            continue
        candidate_page = _cian_page_number(candidate)
        if candidate_page > current_page:
            pages.append((candidate_page, candidate))

    return min(pages, default=(0, None), key=lambda item: item[0])[1]


def _cian_page_number(url: str) -> int:
    raw_page = parse_qs(urlsplit(url).query).get("p", ["1"])[0]
    try:
        return max(int(raw_page), 1)
    except ValueError:
        return 1


def _cian_filtering_diagnostics(
    listings: Sequence[Listing],
    params: SourceSearchParams,
) -> CianFilteringDiagnostics:
    excluded_by_price = 0
    excluded_by_area = 0
    excluded_by_type = 0
    excluded_outside_polygon = 0
    kept_without_coordinates = 0
    result_count = 0
    selected_types = set(params.property_types)

    for listing in listings:
        if listing.property_type not in selected_types:
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
        if listing.latitude is None or listing.longitude is None:
            kept_without_coordinates += 1
            result_count += 1
            continue
        if not point_is_in_polygon(
            listing.latitude,
            listing.longitude,
            list(params.polygon),
        ):
            excluded_outside_polygon += 1
            continue
        result_count += 1

    return CianFilteringDiagnostics(
        total=len(listings),
        excluded_by_price=excluded_by_price,
        excluded_by_area=excluded_by_area,
        excluded_by_type=excluded_by_type,
        excluded_outside_polygon=excluded_outside_polygon,
        kept_without_coordinates=kept_without_coordinates,
        result_count=result_count,
    )


async def _visible_page_links(page: Any) -> list[tuple[Any, str]]:
    try:
        links = page.locator("a[href]")
        count = min(await links.count(), 100)
    except Exception:
        return []

    results: list[tuple[Any, str]] = []
    seen_urls: set[str] = set()
    for index in range(count):
        try:
            link = links.nth(index)
            if not await link.is_visible():
                continue
            href = await link.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue

        listing_url = urljoin(page.url, href)
        if listing_url in seen_urls:
            continue
        seen_urls.add(listing_url)
        results.append((link, listing_url))
    return results


async def _visible_card_containers(page: Any, source_name: str) -> list[Any]:
    selectors = {
        "cian": (
            "[data-name='CardComponent']",
            "[data-name*='Card']",
            "[data-testid*='card' i]",
            "[itemprop='itemListElement']",
            "article",
        ),
        "yandex_realty": (
            "[data-testid*='offer' i]",
            "[data-testid*='card' i]",
            "[data-name*='card' i]",
            "[itemprop='itemListElement']",
            "article",
        ),
    }[source_name]
    for selector in selectors:
        try:
            candidates = page.locator(selector)
            count = min(await candidates.count(), 50)
        except Exception:
            continue
        visible_cards = []
        for index in range(count):
            try:
                card = candidates.nth(index)
                if await card.is_visible():
                    visible_cards.append(card)
            except Exception:
                continue
        if visible_cards:
            return visible_cards
    return []


def _is_source_listing_url(source_name: str, url: str) -> bool:
    parsed_url = urlsplit(url)
    path = parsed_url.path.casefold()
    hostname = (parsed_url.hostname or "").casefold()
    if source_name == "cian":
        return (
            "cian.ru" in hostname
            and re.search(r"/rent/flat/\d+/?$", path) is not None
        )
    return (
        hostname.endswith("realty.yandex.ru")
        and "/offer/" in path
        and "_ssr_fail" not in url.casefold()
    )


async def _card_container_for_link(link: Any) -> Any | None:
    for selector in (
        "xpath=ancestor::*[@data-testid='offer-card'][1]",
        "xpath=ancestor::*[contains(@data-testid, 'offer-card')][1]",
        "xpath=ancestor::*[@data-name='CardComponent'][1]",
        "xpath=ancestor::*[@data-testid or @data-name or @itemprop or @aria-label][1]",
        "xpath=ancestor::article[1]",
        "xpath=ancestor::li[1]",
        "xpath=ancestor::div[1]",
    ):
        try:
            candidates = link.locator(selector)
            if await candidates.count() and await candidates.nth(0).is_visible():
                return candidates.nth(0)
        except Exception:
            continue
    return None


async def _first_visible_text(card: Any, selectors: Sequence[str]) -> str | None:
    for selector in selectors:
        try:
            locator = card.locator(selector)
            if await locator.count() and await locator.nth(0).is_visible():
                text = _compact_text(await locator.nth(0).inner_text())
                if text:
                    return text
        except Exception:
            continue
    return None


async def _coordinates_from_card(card: Any) -> tuple[float | None, float | None]:
    candidates = [card]
    for selector in (
        "[data-latitude][data-longitude]",
        "[data-lat][data-lon]",
        "[data-lat][data-lng]",
        "[data-geo-lat][data-geo-lng]",
        "[data-geo]",
        "[data-location]",
        "[data-coordinates]",
    ):
        try:
            elements = card.locator(selector)
            count = min(await elements.count(), 10)
        except Exception:
            continue
        for index in range(count):
            try:
                element = elements.nth(index)
                if await element.is_visible():
                    candidates.append(element)
            except Exception:
                continue

    for candidate in candidates:
        coordinates = await _coordinates_from_element(candidate)
        if coordinates is not None:
            return coordinates

    for selector in (
        "a[href*='maps' i]",
        "a[href*='map' i]",
        "a[href*='latitude' i]",
        "a[href*='lat=' i]",
    ):
        try:
            links = card.locator(selector)
            count = min(await links.count(), 10)
        except Exception:
            continue
        for index in range(count):
            try:
                href = await links.nth(index).get_attribute("href")
            except Exception:
                continue
            coordinates = _coordinates_from_url(href)
            if coordinates is not None:
                return coordinates

    try:
        outer_html = await card.evaluate("(element) => element.outerHTML")
    except Exception:
        return None, None
    return _coordinates_from_text_data(outer_html) or (None, None)


async def _coordinates_from_element(
    element: Any,
) -> tuple[float, float] | None:
    for latitude_name, longitude_name in (
        ("data-latitude", "data-longitude"),
        ("data-lat", "data-lon"),
        ("data-lat", "data-lng"),
        ("data-geo-lat", "data-geo-lng"),
    ):
        try:
            raw_latitude = await element.get_attribute(latitude_name)
            raw_longitude = await element.get_attribute(longitude_name)
        except Exception:
            continue
        latitude = _number(raw_latitude)
        longitude = _number(raw_longitude)
        if latitude is not None and longitude is not None:
            return latitude, longitude

    for attribute_name in (
        "data-geo",
        "data-location",
        "data-coordinates",
        "data-map",
    ):
        try:
            value = await element.get_attribute(attribute_name)
        except Exception:
            continue
        coordinates = _coordinates_from_text_data(value)
        if coordinates is not None:
            return coordinates
    return None


def _coordinates_from_url(url: str | None) -> tuple[float, float] | None:
    if not url:
        return None
    query = parse_qs(urlsplit(url).query)
    latitude = _number(
        (query.get("latitude") or query.get("lat") or [None])[0]
    )
    longitude = _number(
        (query.get("longitude") or query.get("lng") or query.get("lon") or [None])[0]
    )
    if latitude is not None and longitude is not None:
        return latitude, longitude
    return _coordinates_from_text_data(url)


def _coordinates_from_text_data(
    value: str | None,
) -> tuple[float, float] | None:
    if not value:
        return None

    try:
        parsed = json.loads(html.unescape(value))
    except (json.JSONDecodeError, TypeError):
        parsed = None
    coordinates = _coordinates_from_mapping(parsed)
    if coordinates is not None:
        return coordinates

    latitude_match = re.search(
        r"(?:latitude|lat)\s*[=:]\s*[\"']?(-?\d+(?:[.,]\d+)?)",
        value,
        flags=re.IGNORECASE,
    )
    longitude_match = re.search(
        r"(?:longitude|lng|lon)\s*[=:]\s*[\"']?(-?\d+(?:[.,]\d+)?)",
        value,
        flags=re.IGNORECASE,
    )
    if latitude_match is None or longitude_match is None:
        return None
    latitude = _number(latitude_match.group(1))
    longitude = _number(longitude_match.group(1))
    if latitude is None or longitude is None:
        return None
    return latitude, longitude


def _cian_coordinates_from_page_data(
    page_html: str,
) -> dict[str, tuple[float, float]]:
    coordinates_by_id: dict[str, tuple[float, float]] = {}
    for payload in _json_payloads(page_html):
        for candidate in _walk_mappings(payload):
            external_id = _mapping_external_id(candidate)
            coordinates = _coordinates_from_mapping(candidate)
            if external_id and coordinates is not None:
                coordinates_by_id[external_id] = coordinates
    return coordinates_by_id


def _json_payloads(page_html: str) -> list[Any]:
    payloads: list[Any] = []
    for match in re.finditer(
        r"<script(?P<attributes>[^>]*)>(?P<body>.*?)</script>",
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        attributes = match.group("attributes").casefold()
        body = html.unescape(match.group("body")).strip()
        has_json_type = (
            "application/json" not in attributes
            and "__next_data__" not in attributes
            and "initial-state" not in attributes
        )
        has_initial_state = bool(
            re.search(
                r"(?:window\.)?(?:__next_data__|__initial_state__|initial_state)",
                body,
                flags=re.IGNORECASE,
            )
        )
        if has_json_type and not has_initial_state:
            continue

        candidates = [body]
        assignment = re.search(
            r"=\s*(\{.*\}|\[.*\])\s*;?\s*$",
            body,
            flags=re.DOTALL,
        )
        if assignment is not None:
            candidates.append(assignment.group(1))
        for candidate in candidates:
            if not candidate.startswith(("{", "[")):
                continue
            try:
                payloads.append(json.loads(candidate))
            except json.JSONDecodeError:
                continue
    return payloads


def _walk_mappings(value: Any) -> Sequence[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    if isinstance(value, dict):
        mappings.append(value)
        for child in value.values():
            mappings.extend(_walk_mappings(child))
    elif isinstance(value, list):
        for child in value:
            mappings.extend(_walk_mappings(child))
    return mappings


def _mapping_external_id(value: dict[str, Any]) -> str | None:
    for name in ("external_id", "externalId", "offerId", "id"):
        raw_value = value.get(name)
        if isinstance(raw_value, (str, int)):
            return str(raw_value)
    return None


def _coordinates_from_mapping(
    value: Any,
) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    for candidate in (
        value,
        value.get("geo"),
        value.get("geoData"),
        value.get("location"),
    ):
        if not isinstance(candidate, dict):
            continue
        latitude = _number(candidate.get("latitude", candidate.get("lat")))
        longitude = _number(
            candidate.get(
                "longitude",
                candidate.get("lng", candidate.get("lon")),
            )
        )
        if latitude is not None and longitude is not None:
            return latitude, longitude

        raw_coordinates = candidate.get(
            "coordinates",
            candidate.get("coordinate"),
        )
        if isinstance(raw_coordinates, (list, tuple)) and len(raw_coordinates) >= 2:
            first = _number(raw_coordinates[0])
            second = _number(raw_coordinates[1])
            if first is not None and second is not None:
                if abs(first) <= 90 < abs(second):
                    return first, second
                return second, first
    return None


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


async def _visible_body_text(page: Any) -> str:
    try:
        body = page.locator("body")
        if await body.count() == 0 or not await body.is_visible():
            return ""
        return _compact_text(await body.inner_text())
    except Exception:
        return ""


async def _has_visible_listing_content(
    page: Any,
    source_name: str,
    visible_body_text: str,
) -> bool:
    if await _any_visible_locator(page, _LISTING_CONTENT_SELECTORS[source_name]):
        return True

    normalized_text = visible_body_text.casefold()
    if source_name == "cian" and re.search(
        r"найдено\s+\d[\d\s\u00a0]*\s+объявлен",
        normalized_text,
    ):
        return True

    has_price = bool(
        re.search(
            r"\d[\d\s\u00a0]{2,}\s*(?:₽|руб(?:\.|лей)?)",
            visible_body_text,
            flags=re.IGNORECASE,
        )
    )
    has_property_heading = "студи" in normalized_text or "комн" in normalized_text
    return has_price and has_property_heading


async def _has_visible_captcha(page: Any, visible_body_text: str) -> bool:
    if await _any_visible_locator(page, _VISIBLE_CAPTCHA_SELECTORS):
        return True
    normalized_text = visible_body_text.casefold()
    return any(
        marker in normalized_text for marker in _VISIBLE_CAPTCHA_TEXT_MARKERS
    )


async def _any_visible_locator(page: Any, selectors: Sequence[str]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 20)):
                if await locator.nth(index).is_visible():
                    return True
        except Exception:
            continue
    return False


def _property_type_from_text(text: str) -> str | None:
    normalized_text = text.casefold()
    if "студи" in normalized_text:
        return "studio"
    if "однокомн" in normalized_text or re.search(r"\b1\s*[- ]?ком", normalized_text):
        return "one_room"
    return None


def _price_from_text(text: str) -> int | None:
    match = re.search(
        r"(?<!\d)(\d[\d\s\u00a0]{2,})\s*(?:₽|руб(?:\.|лей)?)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = int(re.sub(r"\D", "", match.group(1)))
    return value if value > 0 else None


def _rental_price_from_card(
    *,
    main_price_text: str | None,
    card_text: str,
) -> int | None:
    """Return only an explicitly marked monthly rent price.

    A visible card can contain deposits, fees and short-term rates.  CIAN cards
    are accepted only when a price is explicitly labelled as monthly; daily
    prices are deliberately never converted to a monthly equivalent.
    """
    for value in (main_price_text or "", card_text):
        price = _explicit_monthly_price_from_text(value)
        if price is not None:
            return price
    return None


def _explicit_monthly_price_from_text(text: str) -> int | None:
    match = re.search(
        r"(?<!\d)(\d[\d\s\u00a0]{2,})\s*(?:₽|руб(?:\.|лей)?)"
        r"\s*(?:/\s*мес(?:\.|яц(?:а|ев)?)?|в\s+месяц|помесячно)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = int(re.sub(r"\D", "", match.group(1)))
    return value if value > 0 else None


def _has_daily_rent_marker(text: str) -> bool:
    return re.search(
        r"(?:₽|руб(?:\.|лей)?)\s*/\s*сут(?:\.|ки)?|"
        r"\b(?:в\s+сутки|посуточно|за\s+сутки|сутки)\b",
        text,
        flags=re.IGNORECASE,
    ) is not None


def _monthly_price_from_text(text: str) -> int | None:
    match = re.search(
        r"(?<!\d)(\d[\d\s\u00a0]{2,})\s*(?:₽|руб(?:\.|лей)?)"
        r"\s*/?\s*(?:мес(?:\.|яц(?:а|ев)?)?)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = int(re.sub(r"\D", "", match.group(1)))
    return value if value > 0 else None


def _unambiguous_main_price(text: str) -> int | None:
    normalized_text = text.casefold()
    if any(
        marker in normalized_text
        for marker in ("залог", "комис", "за м²", "за м2", "за кв", "₽/м²")
    ):
        return None
    return _single_unambiguous_price(text)


def _single_unambiguous_price(text: str) -> int | None:
    if re.search(
        r"(?:/|\bза\s+)(?:сут(?:ки)?|день|ноч[ьи]|час(?:а|ов)?)",
        text,
        flags=re.IGNORECASE,
    ):
        return None
    matches = list(
        re.finditer(
            r"(?<!\d)(\d[\d\s\u00a0]{2,})\s*(?:₽|руб(?:\.|лей)?)",
            text,
            flags=re.IGNORECASE,
        )
    )
    if len(matches) != 1:
        return None
    value = int(re.sub(r"\D", "", matches[0].group(1)))
    return value if value > 0 else None


def _area_from_text(text: str) -> float | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?:²|2|\s*кв\.?\s*м)", text)
    return _number(match.group(1)) if match else None


def _metro_from_text(text: str) -> tuple[str | None, int | None]:
    compact_text = _compact_text(text)
    minutes_match = re.search(
        r"(?<!\d)(\d{1,3})\s*мин(?:ут[аы]?|\.?)?",
        compact_text,
        flags=re.IGNORECASE,
    )
    station_text = (
        compact_text[: minutes_match.start()]
        if minutes_match is not None
        else compact_text
    )
    station = _clean_metro_station(station_text)
    minutes = int(minutes_match.group(1)) if minutes_match is not None else None
    return station, minutes


def _clean_metro_station(value: str) -> str | None:
    station = _compact_text(value)
    station = re.sub(r"^(?:м\.?\s*|метро\s+)", "", station, flags=re.IGNORECASE)
    station = re.sub(
        r"^(?:(?:хорошая цена|без комиссии|только на циан|проверено циан|"
        r"собственник|агентство)\s+)+",
        "",
        station,
        flags=re.IGNORECASE,
    )
    station = re.sub(r"\s*(?:,|·|\||—|-)\s*$", "", station).strip()
    trailing_station = re.search(
        r"([А-ЯЁ][А-ЯЁа-яё-]*(?:\s+[А-ЯЁа-яё-]+){0,4})$",
        station,
    )
    if trailing_station is not None:
        station = trailing_station.group(1)
    if not station:
        return None
    return station


def _address_from_text(text: str) -> str:
    address_match = re.search(
        r"(Москва[^\n]*(?:ул\.|улица|проспект|шоссе|переулок|набережная)[^\n]*)",
        text,
        flags=re.IGNORECASE,
    )
    return _compact_text(address_match.group(1)) if address_match else "Адрес не указан"


def _address_from_card_text(raw_text: str) -> str:
    for line in raw_text.splitlines():
        normalized_line = _compact_text(line)
        if not normalized_line:
            continue
        lowered_line = normalized_line.casefold()
        if any(
            marker in lowered_line
            for marker in (
                "улиц",
                "ул.",
                "проспект",
                "шоссе",
                "переул",
                "набережн",
                "бульвар",
                "проезд",
                "квартал",
                "жк ",
            )
        ):
            return normalized_line
    return _address_from_text(_compact_text(raw_text))


def _title_from_text(text: str) -> str:
    return text[:200] if text else "Объявление"


def _external_id_from_url(url: str) -> str:
    path_parts = [part for part in urlsplit(url).path.split("/") if part]
    if "offer" in path_parts:
        offer_index = path_parts.index("offer")
        if offer_index + 1 < len(path_parts):
            return path_parts[offer_index + 1]
    if "flat" in path_parts:
        flat_index = path_parts.index("flat")
        if flat_index + 1 < len(path_parts):
            return path_parts[flat_index + 1]
    match = re.search(r"(?:offer|flat|rent|id)[^0-9]*(\d+)", url, flags=re.IGNORECASE)
    if match is not None:
        return match.group(1)
    return f"browser-{sha256(url.encode('utf-8')).hexdigest()[:16]}"


def _number(value: str | int | float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            return float(value.replace(",", "."))
        return float(value)
    except (TypeError, ValueError):
        return None
