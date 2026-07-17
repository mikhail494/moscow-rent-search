import asyncio
import html
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from app.models import Listing
from app.services.filtering import filter_listings

from .base import SearchSource, SourceSearchParams, SourceSearchResult


logger = logging.getLogger(__name__)


class YandexRealtyParseError(ValueError):
    pass


@dataclass(frozen=True)
class _FetchedPage:
    url: str
    status_code: int
    html: str


class _MetaRefreshParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.content: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() != "meta" or self.content is not None:
            return

        attributes = {name.casefold(): value for name, value in attrs}
        if attributes.get("http-equiv", "").casefold() != "refresh":
            return

        content = attributes.get("content")
        if content is not None:
            self.content = content


class YandexRealtySource(SearchSource):
    name = "yandex_realty"
    base_url = "https://realty.yandex.ru/moskva/snyat/kvartira/"
    _allowed_hosts = frozenset({"realty.yandex.ru"})
    _script_pattern = re.compile(
        r"<script(?P<attributes>[^>]*)>(?P<body>.*?)</script>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _meta_refresh_url_pattern = re.compile(
        r"(?:^|;)\s*url\s*=\s*(?P<url>.+?)\s*$", flags=re.IGNORECASE
    )
    _state_markers = (
        "__NEXT_DATA__",
        "__INITIAL_STATE__",
        "__PRELOADED_STATE__",
        "window.INITIAL_STATE",
    )
    _captcha_markers = (
        "g-recaptcha",
        "h-captcha",
        "smartcaptcha",
        "captcha_smart",
        "checkcaptcha",
        "captcha-container",
        "вы не робот",
        "подтвердите, что вы не робот",
        "проверка безопасности",
    )
    _empty_result_markers = (
        "ничего не найдено",
        "по вашему запросу ничего не найдено",
        "нет подходящих объявлений",
    )

    def __init__(self, debug_path: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.debug_path = debug_path or project_root / "debug" / "yandex_realty_last.html"

    async def search(self, params: SourceSearchParams) -> SourceSearchResult:
        first_url = self.build_search_url(params)

        try:
            first_page = await asyncio.to_thread(self._fetch_page, first_url)
        except HTTPError as error:
            return self._http_error_result(error)
        except (TimeoutError, URLError, OSError):
            return self._network_error_result()

        first_page_result = self._captcha_or_empty_result(first_page.html)
        if first_page_result is not None:
            return first_page_result

        try:
            refresh_url = self._meta_refresh_url(first_page.html, first_page.url)
        except YandexRealtyParseError as error:
            return self._parse_error_result(error, first_page.html)

        if refresh_url is None:
            return self._parse_page(first_page, params)

        try:
            second_page = await asyncio.to_thread(self._fetch_page, refresh_url)
        except HTTPError as error:
            return self._http_error_result(error, after_meta_refresh=True)
        except (TimeoutError, URLError, OSError):
            return self._network_error_result(after_meta_refresh=True)

        logger.warning(
            "Yandex Realty meta refresh followed: first_url=%s refresh_url=%s second_status=%s",
            first_page.url,
            refresh_url,
            second_page.status_code,
        )

        second_page_result = self._captcha_or_empty_result(
            second_page.html, allow_empty_results=False
        )
        if second_page_result is not None:
            return second_page_result

        try:
            repeated_refresh_url = self._meta_refresh_url(
                second_page.html, second_page.url
            )
        except YandexRealtyParseError as error:
            return self._parse_error_result(error, second_page.html)

        if repeated_refresh_url is not None:
            return self._parse_error_result(
                YandexRealtyParseError(
                    "страница после meta refresh снова содержит meta refresh; "
                    "второй переход не выполняется"
                ),
                second_page.html,
            )

        return self._parse_page(second_page, params, after_meta_refresh=True)

    def _http_error_result(
        self, error: HTTPError, *, after_meta_refresh: bool = False
    ) -> SourceSearchResult:
        request_stage = "после meta refresh " if after_meta_refresh else ""
        if error.code in {403, 429}:
            return SourceSearchResult(
                status="blocked",
                total_before_filtering=0,
                listings=[],
                error=(
                    "Яндекс Недвижимость отклонила запрос "
                    f"{request_stage}HTTP {error.code}."
                ),
            )
        return SourceSearchResult(
            status="unavailable",
            total_before_filtering=0,
            listings=[],
            error=(
                "Яндекс Недвижимость недоступна "
                f"{request_stage}HTTP {error.code}."
            ),
        )

    def _network_error_result(
        self, *, after_meta_refresh: bool = False
    ) -> SourceSearchResult:
        request_stage = "после meta refresh " if after_meta_refresh else ""
        return SourceSearchResult(
            status="unavailable",
            total_before_filtering=0,
            listings=[],
            error=(
                "Не удалось получить страницу Яндекс Недвижимости "
                f"{request_stage}Попробуйте позже."
            ),
        )

    def _captcha_or_empty_result(
        self, page_html: str, *, allow_empty_results: bool = True
    ) -> SourceSearchResult | None:
        if self._looks_like_captcha(page_html):
            return SourceSearchResult(
                status="captcha",
                total_before_filtering=0,
                listings=[],
                error="Яндекс Недвижимость запросила CAPTCHA. Автоматическая обработка не выполняется.",
            )
        if allow_empty_results and self._looks_like_empty_results(page_html):
            return SourceSearchResult(
                status="ok",
                total_before_filtering=0,
                listings=[],
            )
        return None

    def _parse_page(
        self,
        page: _FetchedPage,
        params: SourceSearchParams,
        *,
        after_meta_refresh: bool = False,
    ) -> SourceSearchResult:
        try:
            raw_listings = self.parse_listings(page.html)
        except YandexRealtyParseError as error:
            prefix = (
                f"после meta refresh (HTTP {page.status_code})"
                if after_meta_refresh
                else ""
            )
            return self._parse_error_result(error, page.html, prefix=prefix)

        return SourceSearchResult(
            status="ok",
            total_before_filtering=len(raw_listings),
            listings=filter_listings(raw_listings, params),
        )

    def _parse_error_result(
        self, error: YandexRealtyParseError, page_html: str, *, prefix: str = ""
    ) -> SourceSearchResult:
        self._save_parse_debug(page_html)
        context = f" {prefix}" if prefix else ""
        return SourceSearchResult(
            status="parse_error",
            total_before_filtering=0,
            listings=[],
            error=f"Не удалось разобрать страницу Яндекс Недвижимости{context}: {error}",
        )

    def _meta_refresh_url(self, page_html: str, response_url: str) -> str | None:
        parser = _MetaRefreshParser()
        parser.feed(page_html)
        parser.close()

        if parser.content is None:
            return None

        content = html.unescape(parser.content).strip()
        match = self._meta_refresh_url_pattern.search(content)
        if match is None:
            raise YandexRealtyParseError(
                "meta refresh не содержит корректный URL"
            )

        raw_url = html.unescape(match.group("url")).strip().strip("'\"")
        if not raw_url:
            raise YandexRealtyParseError("meta refresh содержит пустой URL")

        target_url = urljoin(response_url, raw_url)
        try:
            parsed_url = urlsplit(target_url)
        except ValueError as error:
            raise YandexRealtyParseError(
                "meta refresh содержит некорректный URL"
            ) from error

        if (
            parsed_url.scheme not in {"http", "https"}
            or parsed_url.hostname not in self._allowed_hosts
        ):
            raise YandexRealtyParseError(
                "meta refresh ведёт на недопустимый URL"
            )
        return target_url

    def build_search_url(self, params: SourceSearchParams) -> str:
        query: list[tuple[str, str]] = [("priceMax", str(params.max_price))]
        room_values: list[str] = []
        if "studio" in params.property_types:
            room_values.append("studio")
        if "one_room" in params.property_types:
            room_values.append("1")
        if room_values:
            query.append(("roomsTotal", ",".join(room_values)))
        if params.min_area is not None:
            query.append(("areaMin", str(params.min_area)))
        if params.max_area is not None:
            query.append(("areaMax", str(params.max_area)))
        return f"{self.base_url}?{urlencode(query)}"

    def parse_listings(self, page_html: str) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for payload in self._json_payloads(page_html):
            for candidate in self._walk_dicts(payload):
                listing = self._normalize_listing(candidate)
                if listing is None or listing.external_id in seen_ids:
                    continue
                seen_ids.add(listing.external_id)
                listings.append(listing)

        if not listings:
            raise YandexRealtyParseError("карточки объявлений в ожидаемом формате не найдены")
        return listings

    def _fetch_page(self, url: str) -> _FetchedPage:
        request = Request(
            url,
            headers={
                "User-Agent": "MoscowRentSearch/0.1 (local demo)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.8",
            },
        )
        with urlopen(request, timeout=15) as response:
            encoding = response.headers.get_content_charset() or "utf-8"
            return _FetchedPage(
                url=response.geturl(),
                status_code=response.status,
                html=response.read().decode(encoding, errors="replace"),
            )

    def _looks_like_captcha(self, page_html: str) -> bool:
        normalized_html = page_html.casefold()
        return any(marker in normalized_html for marker in self._captcha_markers)

    def _looks_like_empty_results(self, page_html: str) -> bool:
        normalized_html = page_html.casefold()
        return any(marker in normalized_html for marker in self._empty_result_markers)

    def _json_payloads(self, page_html: str) -> Iterator[Any]:
        for match in self._script_pattern.finditer(page_html):
            attributes = match.group("attributes").casefold()
            if "application/json" not in attributes and "__next_data__" not in attributes:
                continue

            body = html.unescape(match.group("body")).strip()
            if not body.startswith(("{", "[")):
                continue
            try:
                yield json.loads(body)
            except json.JSONDecodeError:
                continue

        decoder = json.JSONDecoder()
        for marker in self._state_markers:
            marker_index = page_html.find(marker)
            if marker_index == -1:
                continue
            payload_start = page_html.find("{", marker_index)
            if payload_start == -1:
                continue
            try:
                payload, _ = decoder.raw_decode(page_html[payload_start:])
            except json.JSONDecodeError:
                continue
            yield payload

    def _walk_dicts(self, value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_dicts(child)

    def _normalize_listing(self, candidate: dict[str, Any]) -> Listing | None:
        external_id = self._string_value(
            self._first_value(candidate, "externalId", "offerId", "id", "uuid")
        )
        price = self._number_value(
            self._first_value(candidate, "price", "priceValue", "rentPrice", "amount")
        )
        area = self._number_value(
            self._first_value(candidate, "totalArea", "area", "areaValue", "areaSqm")
        )
        property_type = self._property_type(candidate)

        if not external_id or price is None or area is None or property_type is None:
            return None

        url = self._listing_url(candidate, external_id)
        if url is None:
            return None

        title = self._string_value(
            self._first_value(candidate, "title", "headline", "description", "name")
        ) or "Объявление Яндекс Недвижимости"
        latitude, longitude = self._coordinates(candidate)
        metro_station, metro_minutes = self._metro(candidate)
        address = self._string_value(
            self._first_value(candidate, "address", "fullAddress", "addressText")
        )
        location = candidate.get("location")
        if address is None and isinstance(location, dict):
            address = self._string_value(
                self._first_value(location, "address", "geocoderAddress", "streetAddress")
            )

        try:
            return Listing(
                source="yandex_realty",
                external_id=external_id,
                url=url,
                title=title,
                property_type=property_type,
                rent_price=int(price),
                area_sqm=area,
                metro_station=metro_station,
                metro_minutes=metro_minutes,
                address=address or "Адрес не указан",
                latitude=latitude,
                longitude=longitude,
                location_verified=latitude is not None and longitude is not None,
            )
        except ValueError:
            return None

    def _listing_url(self, candidate: dict[str, Any], external_id: str) -> str | None:
        raw_url = self._string_value(
            self._first_value(candidate, "url", "fullUrl", "offerUrl", "href", "link")
        )
        if raw_url:
            return urljoin("https://realty.yandex.ru", raw_url)
        return f"https://realty.yandex.ru/offer/{external_id}/"

    def _property_type(self, candidate: dict[str, Any]) -> str | None:
        room_key = self._string_value(
            self._first_value(candidate, "roomsTotalKey", "roomKey")
        )
        if room_key and room_key.casefold() == "studio":
            return "studio"

        room_count = self._number_value(
            self._first_value(
                candidate,
                "roomsCount",
                "roomCount",
                "roomsTotal",
                "rooms",
                "room",
            )
        )
        if room_count == 0 or candidate.get("isStudio") is True:
            return "studio"
        if room_count == 1:
            return "one_room"

        title = self._string_value(
            self._first_value(candidate, "title", "headline", "description", "name")
        )
        if not title:
            return None
        normalized_title = title.casefold()
        if "студи" in normalized_title:
            return "studio"
        if "1-комн" in normalized_title or "однокомнат" in normalized_title:
            return "one_room"
        return None

    def _coordinates(self, candidate: dict[str, Any]) -> tuple[float | None, float | None]:
        geo = self._first_value(candidate, "geo", "geoData", "location", "coordinates")
        location = geo if isinstance(geo, dict) else candidate
        latitude = self._number_value(self._first_value(location, "latitude", "lat"))
        longitude = self._number_value(self._first_value(location, "longitude", "lng", "lon"))

        point = self._first_value(location, "point", "coordinates")
        if isinstance(point, dict):
            latitude = latitude or self._number_value(
                self._first_value(point, "latitude", "lat")
            )
            longitude = longitude or self._number_value(
                self._first_value(point, "longitude", "lng", "lon")
            )

        coordinates = geo if isinstance(geo, list) else point
        if (latitude is None or longitude is None) and isinstance(coordinates, list):
            if len(coordinates) >= 2:
                first = self._number_value(coordinates[0])
                second = self._number_value(coordinates[1])
                if first is not None and second is not None:
                    if abs(first) <= 90 and abs(second) > 90:
                        latitude, longitude = first, second
                    else:
                        longitude, latitude = first, second

        if latitude is None or longitude is None:
            return None, None
        return latitude, longitude

    def _metro(self, candidate: dict[str, Any]) -> tuple[str | None, int | None]:
        undergrounds = self._first_value(candidate, "undergrounds", "underground", "metro")
        location = candidate.get("location")
        if undergrounds is None and isinstance(location, dict):
            undergrounds = self._first_value(location, "metro", "metroList", "undergrounds")
        if isinstance(undergrounds, list) and undergrounds:
            underground = undergrounds[0]
        else:
            underground = undergrounds

        if isinstance(underground, str):
            return underground, None
        if not isinstance(underground, dict):
            return None, None

        station = self._string_value(
            self._first_value(underground, "name", "stationName", "title")
        )
        minutes = self._number_value(
            self._first_value(
                underground,
                "time",
                "travelTime",
                "timeToMetro",
                "minTimeToMetro",
                "minutes",
            )
        )
        return station, int(minutes) if minutes is not None else None

    def _first_value(self, value: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in value and value[key] is not None:
                return value[key]
        return None

    def _string_value(self, value: Any) -> str | None:
        if isinstance(value, str):
            return re.sub(r"\s+", " ", html.unescape(value)).strip() or None
        if isinstance(value, dict):
            return self._string_value(
                self._first_value(value, "value", "text", "name", "fullAddress")
            )
        return None

    def _number_value(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            return self._number_value(
                self._first_value(value, "value", "amount", "rur", "price")
            )
        if isinstance(value, str):
            match = re.search(r"\d+(?:[.,]\d+)?", value.replace(" ", ""))
            return float(match.group(0).replace(",", ".")) if match else None
        return None

    def _save_parse_debug(self, page_html: str) -> None:
        self.debug_path.parent.mkdir(parents=True, exist_ok=True)
        self.debug_path.write_text(page_html, encoding="utf-8")
