import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.sources.base import SourceSearchParams
from app.sources.yandex_realty import (
    YandexRealtyParseError,
    YandexRealtySource,
    _FetchedPage,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "yandex_realty_search.html"
INITIAL_STATE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "yandex_realty_initial_state.html"
)


class YandexRealtyParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.source = YandexRealtySource(
            debug_path=Path(self.temporary_directory.name) / "last.html"
        )
        self.fixture_html = FIXTURE_PATH.read_text(encoding="utf-8")
        self.params = SourceSearchParams(
            property_types=["studio", "one_room"],
            min_area=18,
            max_area=50,
            max_price=90000,
            polygon=[
                (37.4, 55.5),
                (38.0, 55.5),
                (38.0, 56.0),
                (37.4, 56.0),
                (37.4, 55.5),
            ],
        )

    def test_parses_normalized_listings_from_fixture(self) -> None:
        listings = self.source.parse_listings(self.fixture_html)

        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[0].external_id, "yandex-1001")
        self.assertEqual(listings[0].property_type, "studio")
        self.assertEqual(listings[0].rent_price, 68000)
        self.assertEqual(listings[0].metro_station, "Бауманская")
        self.assertTrue(listings[0].location_verified)
        self.assertEqual(listings[1].property_type, "one_room")
        self.assertIsNone(listings[1].latitude)
        self.assertFalse(listings[1].location_verified)

    def test_recognizes_captcha_page(self) -> None:
        captcha_html = "<form action='/checkcaptcha'><script src='/captcha_smart.js'></script></form>"

        self.assertTrue(self.source._looks_like_captcha(captcha_html))

    def test_parses_initial_state_shape(self) -> None:
        initial_state_html = INITIAL_STATE_FIXTURE_PATH.read_text(encoding="utf-8")

        listings = self.source.parse_listings(initial_state_html)

        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].external_id, "yandex-live-shape-1")
        self.assertEqual(listings[0].property_type, "studio")
        self.assertEqual(listings[0].address, "Москва, Примерная улица, 10")
        self.assertEqual(listings[0].metro_station, "Тверская")
        self.assertEqual(listings[0].metro_minutes, 8)
        self.assertTrue(listings[0].location_verified)

    def test_builds_url_with_public_filters(self) -> None:
        url = self.source.build_search_url(
            SourceSearchParams(
                property_types=["studio", "one_room"],
                min_area=20,
                max_area=40,
                max_price=90000,
                polygon=[(37.6, 55.7), (37.7, 55.7), (37.7, 55.8)],
            )
        )

        self.assertIn("priceMax=90000", url)
        self.assertIn("roomsTotal=studio%2C1", url)
        self.assertIn("areaMin=20", url)
        self.assertIn("areaMax=40", url)

    def test_recognizes_empty_result_page(self) -> None:
        self.assertTrue(
            self.source._looks_like_empty_results("<main>По вашему запросу ничего не найдено</main>")
        )

    def test_rejects_unrecognized_html(self) -> None:
        with self.assertRaises(YandexRealtyParseError):
            self.source.parse_listings("<html><body>Нет карточек</body></html>")

    def test_resolves_relative_meta_refresh_url_and_decodes_entities(self) -> None:
        target_url = self.source._meta_refresh_url(
            '<meta http-equiv="refresh" content="0; url=/moskva/snyat/kvartira/?priceMax=90000&amp;areaMax=50">',
            "https://realty.yandex.ru/moskva/snyat/kvartira/?priceMax=90000",
        )

        self.assertEqual(
            target_url,
            "https://realty.yandex.ru/moskva/snyat/kvartira/?priceMax=90000&areaMax=50",
        )

    def test_accepts_absolute_meta_refresh_url(self) -> None:
        target_url = self.source._meta_refresh_url(
            '<meta http-equiv="REFRESH" content="0; URL=https://realty.yandex.ru/moskva/snyat/kvartira/?page=2&amp;priceMax=90000">',
            "https://realty.yandex.ru/moskva/snyat/kvartira/",
        )

        self.assertEqual(
            target_url,
            "https://realty.yandex.ru/moskva/snyat/kvartira/?page=2&priceMax=90000",
        )

    def test_rejects_meta_refresh_to_another_domain(self) -> None:
        with self.assertRaisesRegex(YandexRealtyParseError, "недопустимый URL"):
            self.source._meta_refresh_url(
                '<meta http-equiv="refresh" content="0; url=https://example.test/listings">',
                "https://realty.yandex.ru/moskva/snyat/kvartira/",
            )

    def test_stops_after_a_second_meta_refresh(self) -> None:
        initial_url = self.source.build_search_url(self.params)
        refresh_url = "https://realty.yandex.ru/moskva/snyat/kvartira/?_ssr_fail_status_code=404"
        pages = [
            _FetchedPage(
                url=initial_url,
                status_code=200,
                html=f'<meta http-equiv="refresh" content="0; url={refresh_url}">',
            ),
            _FetchedPage(
                url=refresh_url,
                status_code=200,
                html=f'<meta http-equiv="refresh" content="0; url={refresh_url}&retry=1">',
            ),
        ]

        with patch.object(self.source, "_fetch_page", side_effect=pages) as fetch_page:
            result = asyncio.run(self.source.search(self.params))

        self.assertEqual(result.status, "parse_error")
        self.assertIn("второй переход не выполняется", result.error or "")
        self.assertEqual(fetch_page.call_count, 2)

    def test_parses_listings_after_one_meta_refresh(self) -> None:
        initial_url = self.source.build_search_url(self.params)
        refresh_url = "https://realty.yandex.ru/moskva/snyat/kvartira/?_ssr_fail_status_code=404"
        pages = [
            _FetchedPage(
                url=initial_url,
                status_code=200,
                html=f'<meta http-equiv="refresh" content="0; url={refresh_url}">',
            ),
            _FetchedPage(
                url=refresh_url,
                status_code=200,
                html=self.fixture_html,
            ),
        ]

        with patch.object(self.source, "_fetch_page", side_effect=pages) as fetch_page:
            result = asyncio.run(self.source.search(self.params))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.total_before_filtering, 2)
        self.assertEqual(len(result.listings), 2)
        self.assertEqual(fetch_page.call_count, 2)
        self.assertEqual(fetch_page.call_args_list[1].args[0], refresh_url)


if __name__ == "__main__":
    unittest.main()
