import unittest

from app.models import Listing
from app.services.filtering import filter_listings
from app.sources.base import SourceSearchParams


class FilteringTests(unittest.TestCase):
    def test_regular_web_polygon_filter_keeps_inside_and_unverified_listings_in_display_order(self) -> None:
        listings = [
            Listing(
                source="test",
                external_id="verified-expensive",
                url="https://example.test/verified-expensive",
                title="Verified expensive",
                property_type="studio",
                rent_price=75000,
                area_sqm=24,
                metro_station=None,
                metro_minutes=None,
                address="Inside polygon",
                latitude=55.75,
                longitude=37.62,
                location_verified=True,
            ),
            Listing(
                source="test",
                external_id="unverified-cheap",
                url="https://example.test/unverified-cheap",
                title="Unverified cheap",
                property_type="studio",
                rent_price=45000,
                area_sqm=20,
                metro_station=None,
                metro_minutes=None,
                address="Unknown location",
                latitude=None,
                longitude=None,
                location_verified=False,
            ),
            Listing(
                source="test",
                external_id="verified-cheap",
                url="https://example.test/verified-cheap",
                title="Verified cheap",
                property_type="one_room",
                rent_price=55000,
                area_sqm=35,
                metro_station=None,
                metro_minutes=None,
                address="Inside polygon",
                latitude=55.76,
                longitude=37.63,
                location_verified=True,
            ),
            Listing(
                source="test",
                external_id="outside",
                url="https://example.test/outside",
                title="Outside polygon",
                property_type="studio",
                rent_price=40000,
                area_sqm=19,
                metro_station=None,
                metro_minutes=None,
                address="Outside polygon",
                latitude=55.9,
                longitude=37.9,
                location_verified=True,
            ),
        ]
        params = SourceSearchParams(
            property_types=["studio", "one_room"],
            min_area=18,
            max_area=50,
            max_price=90000,
            polygon=[
                (37.5, 55.7),
                (37.7, 55.7),
                (37.7, 55.8),
                (37.5, 55.8),
            ],
        )

        results = filter_listings(listings, params)

        self.assertEqual(
            [listing.external_id for listing in results],
            ["verified-cheap", "verified-expensive", "unverified-cheap"],
        )
        self.assertTrue(results[0].location_verified)
        self.assertFalse(results[-1].location_verified)


if __name__ == "__main__":
    unittest.main()
