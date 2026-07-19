from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Sequence

from pydantic import BaseModel

from app.models import Listing


PropertyType = Literal["studio", "one_room"]
SourceStatus = Literal["ok", "blocked", "captcha", "parse_error", "unavailable"]


@dataclass(frozen=True)
class SourceSearchParams:
    property_types: Sequence[PropertyType]
    min_area: float | None
    max_area: float | None
    max_price: int
    polygon: Sequence[tuple[float, float]]
    metro_stations: Sequence[str] = ()
    cian_search_url: str | None = None


class SourceSearchResult(BaseModel):
    status: SourceStatus
    total_before_filtering: int
    listings: list[Listing]
    error: str | None = None


class SearchSource(ABC):
    name: str

    @abstractmethod
    async def search(self, params: SourceSearchParams) -> SourceSearchResult:
        """Return normalized listings without raising source-specific failures."""
