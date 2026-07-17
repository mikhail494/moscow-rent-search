from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.models import Listing

if TYPE_CHECKING:
    from app.sources.base import SourceSearchParams


def _point_on_segment(
    point_x: float,
    point_y: float,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> bool:
    cross_product = (point_y - start_y) * (end_x - start_x) - (
        point_x - start_x
    ) * (end_y - start_y)
    if abs(cross_product) > 1e-10:
        return False

    return (
        min(start_x, end_x) <= point_x <= max(start_x, end_x)
        and min(start_y, end_y) <= point_y <= max(start_y, end_y)
    )


def point_is_in_polygon(
    latitude: float,
    longitude: float,
    polygon: list[tuple[float, float]] | tuple[tuple[float, float], ...],
) -> bool:
    point_x, point_y = longitude, latitude
    inside = False

    for index, (start_x, start_y) in enumerate(polygon):
        end_x, end_y = polygon[(index + 1) % len(polygon)]

        if _point_on_segment(point_x, point_y, start_x, start_y, end_x, end_y):
            return True

        intersects = (start_y > point_y) != (end_y > point_y)
        if intersects:
            intersection_x = (end_x - start_x) * (point_y - start_y) / (
                end_y - start_y
            ) + start_x
            if point_x < intersection_x:
                inside = not inside

    return inside


def filter_listings(
    listings: Iterable[Listing], params: "SourceSearchParams"
) -> list[Listing]:
    selected_types = set(params.property_types)
    polygon = list(params.polygon)
    results: list[Listing] = []

    for listing in listings:
        if listing.property_type not in selected_types:
            continue
        if params.min_area is not None and listing.area_sqm < params.min_area:
            continue
        if params.max_area is not None and listing.area_sqm > params.max_area:
            continue
        if listing.rent_price > params.max_price:
            continue

        if listing.latitude is None or listing.longitude is None:
            results.append(listing.model_copy(update={"location_verified": False}))
            continue

        if point_is_in_polygon(listing.latitude, listing.longitude, polygon):
            results.append(listing.model_copy(update={"location_verified": True}))

    return sorted(
        results,
        key=lambda listing: (
            not listing.location_verified,
            listing.rent_price,
        ),
    )
