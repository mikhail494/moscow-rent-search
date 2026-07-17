from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Listing(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    external_id: str
    url: str
    title: str
    property_type: Literal["studio", "one_room"]
    rent_price: int = Field(gt=0)
    area_sqm: float = Field(gt=0)
    metro_station: str | None = None
    metro_minutes: int | None = Field(default=None, ge=0)
    address: str
    latitude: float | None = None
    longitude: float | None = None
    location_verified: bool = False

    @model_validator(mode="after")
    def validate_coordinate_pair(self) -> "Listing":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Latitude and longitude must be specified together.")
        return self
