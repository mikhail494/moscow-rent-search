import asyncio
import html
import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from app.models import Listing
from app.services.filtering import filter_listings

from .base import SearchSource, SourceSearchParams, SourceSearchResult


class CianParseError(ValueError):
    pass


class CianSource(SearchSource):
    name = "cian"
    base_url = "https://www.cian.ru/cat.php"
    _script_pattern = re.compile(
        r"<script(?P<attributes>[^>]*)>(?P<body>.*?)</script>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    _captcha_markers = (
        "g-recaptcha",
        "h-captcha",
        "smartcaptcha",
        "checkcaptcha",
        "captcha-container",
        "data-testid=\"captcha",
        "я не робот",
        "вы не робот",
        "подтвердите, что вы не робот",
        "запросы с вашего устройства похожи на автоматические",
        "проверка безопасности",
    )

    def __init__(self, debug_path: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.debug_path = debug_path or project_root / "debug" / "cian_last.html"

    async def search(self, params: SourceSearchParams) -> SourceSearchResult:
        url = self.build_search_url(params)

        try:
            page_html = await asyncio.to_thread(self._fetch_html, url)
        except HTTPError as error:
            if error.code in {403, 429}:
                return SourceSearchResult(
                    status="blocked",
                    total_before_filtering=0,
                    listings=[],
                    error=f"ЦИАН отклонил запрос: HTTP {error.code}.",
                )
            return SourceSearchResult(
                status="unavailable",
                total_before_filtering=0,
                listings=[],
                error=f"ЦИАН недоступен: HTTP {error.code}.",
            )
        except (TimeoutError, URLError, OSError):
            return SourceSearchResult(
                status="unavailable",
                total_before_filtering=0,
                listings=[],
                error="Не удалось получить страницу ЦИАН. Попробуйте позже.",
            )

        if self._looks_like_captcha(page_html):
            return SourceSearchResult(
                status="captcha",
                total_before_filtering=0,
                listings=[],
                error="ЦИАН запросил CAPTCHA. Автоматическая обработка не выполняется.",
            )

        try:
            raw_listings = self.parse_listings(page_html)
        except CianParseError as error:
            self._save_parse_debug(page_html)
            return SourceSearchResult(
                status="parse_error",
                total_before_filtering=0,
                listings=[],
                error=f"Не удалось разобрать страницу ЦИАН: {error}",
            )

        return SourceSearchResult(
            status="ok",
            total_before_filtering=len(raw_listings),
            listings=filter_listings(raw_listings, params),
        )

    def build_search_url(self, params: SourceSearchParams) -> str:
        query: list[tuple[str, str]] = [
            ("deal_type", "rent"),
            ("engine_version", "2"),
            ("offer_type", "flat"),
            ("region", "1"),
            ("maxprice", str(params.max_price)),
        ]
        if "studio" in params.property_types:
            query.append(("room9", "1"))
        if "one_room" in params.property_types:
            query.append(("room1", "1"))
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
            raise CianParseError("карточки объявлений в ожидаемом формате не найдены")
        return listings

    def _fetch_html(self, url: str) -> str:
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
            return response.read().decode(encoding, errors="replace")

    def _looks_like_captcha(self, page_html: str) -> bool:
        normalized_html = page_html.casefold()
        return any(marker in normalized_html for marker in self._captcha_markers)

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
            self._first_value(candidate, "external_id", "externalId", "offerId", "id")
        )
        price = self._number_value(
            self._first_value(
                candidate,
                "price",
                "priceRur",
                "rentPrice",
                "totalPrice",
            )
        )
        area = self._number_value(
            self._first_value(candidate, "totalArea", "area", "areaSqm", "area_sqm")
        )
        property_type = self._property_type(candidate)

        if not external_id or price is None or area is None or property_type is None:
            return None

        title = self._string_value(
            self._first_value(candidate, "title", "headline", "description", "name")
        ) or "Объявление ЦИАН"
        url = self._listing_url(candidate, external_id)
        if url is None:
            return None

        latitude, longitude = self._coordinates(candidate)
        metro_station, metro_minutes = self._metro(candidate)
        address = self._string_value(self._first_value(candidate, "address", "fullAddress"))

        try:
            return Listing(
                source="cian",
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
            self._first_value(candidate, "url", "fullUrl", "urlPath", "link")
        )
        if raw_url:
            return urljoin("https://www.cian.ru", raw_url)
        return f"https://www.cian.ru/rent/flat/{external_id}/"

    def _property_type(self, candidate: dict[str, Any]) -> str | None:
        room_count = self._number_value(
            self._first_value(candidate, "roomsCount", "roomCount", "rooms", "room_count")
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
        geo = self._first_value(candidate, "geo", "geoData", "location")
        location = geo if isinstance(geo, dict) else candidate
        latitude = self._number_value(self._first_value(location, "latitude", "lat"))
        longitude = self._number_value(self._first_value(location, "longitude", "lng", "lon"))

        coordinates = self._first_value(location, "coordinates", "coordinate")
        if (latitude is None or longitude is None) and isinstance(coordinates, list):
            if len(coordinates) >= 2:
                first, second = coordinates[0], coordinates[1]
                first_number = self._number_value(first)
                second_number = self._number_value(second)
                if first_number is not None and second_number is not None:
                    if abs(first_number) <= 90 and abs(second_number) > 90:
                        latitude, longitude = first_number, second_number
                    else:
                        longitude, latitude = first_number, second_number

        if latitude is None or longitude is None:
            return None, None
        return latitude, longitude

    def _metro(self, candidate: dict[str, Any]) -> tuple[str | None, int | None]:
        undergrounds = self._first_value(candidate, "undergrounds", "underground", "metro")
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
            self._first_value(underground, "time", "travelTime", "minutes")
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
