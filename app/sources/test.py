from app.services.search import TEST_LISTINGS
from app.services.filtering import filter_listings

from .base import SearchSource, SourceSearchParams, SourceSearchResult


class TestSource(SearchSource):
    name = "test"

    async def search(self, params: SourceSearchParams) -> SourceSearchResult:
        listings = filter_listings(TEST_LISTINGS, params)
        return SourceSearchResult(
            status="ok",
            total_before_filtering=len(TEST_LISTINGS),
            listings=listings,
        )
