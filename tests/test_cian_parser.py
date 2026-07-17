import unittest
from pathlib import Path

from app.sources.cian import CianParseError, CianSource


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cian_search.html"


class CianParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = CianSource()
        self.fixture_html = FIXTURE_PATH.read_text(encoding="utf-8")

    def test_parses_normalized_listings_from_fixture(self) -> None:
        listings = self.source.parse_listings(self.fixture_html)

        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[0].external_id, "981234567")
        self.assertEqual(listings[0].property_type, "studio")
        self.assertEqual(listings[0].rent_price, 65000)
        self.assertEqual(listings[0].metro_station, "Парк культуры")
        self.assertTrue(listings[0].location_verified)
        self.assertEqual(listings[1].property_type, "one_room")
        self.assertIsNone(listings[1].latitude)
        self.assertFalse(listings[1].location_verified)

    def test_rejects_unrecognized_html(self) -> None:
        with self.assertRaises(CianParseError):
            self.source.parse_listings("<html><body>Нет карточек</body></html>")

    def test_recognizes_captcha_page(self) -> None:
        captcha_html = "<form action='/checkcaptcha'><script src='/captcha_smart.js'></script></form>"

        self.assertTrue(self.source._looks_like_captcha(captcha_html))


if __name__ == "__main__":
    unittest.main()
