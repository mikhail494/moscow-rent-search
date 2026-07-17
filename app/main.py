from pathlib import Path

from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import Listing
from app.services.export import (
    ExportSearch,
    NoExportResultsError,
    create_excel_export,
    create_html_export,
)
from app.sources.base import SourceSearchParams, SourceStatus
from app.sources.cian import CianSource
from app.sources.test import TestSource
from app.sources.yandex_realty import YandexRealtySource


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR.parent / "output"

app = FastAPI(title="Moscow Rent Search")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.state.last_search_for_export: ExportSearch | None = None

templates = Jinja2Templates(directory=BASE_DIR / "templates")

SOURCES = {
    "test": TestSource(),
    "cian": CianSource(),
    "yandex_realty": YandexRealtySource(),
}


class PolygonGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["Polygon"]
    coordinates: list[list[tuple[float, float]]]

    @field_validator("coordinates")
    @classmethod
    def validate_coordinates(
        cls, coordinates: list[list[tuple[float, float]]]
    ) -> list[list[tuple[float, float]]]:
        if not coordinates or len(coordinates[0]) < 4:
            raise ValueError("Polygon must contain at least three points.")

        if coordinates[0][0] != coordinates[0][-1]:
            raise ValueError("Polygon ring must be closed.")

        return coordinates


class PolygonFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["Feature"]
    geometry: PolygonGeometry
    properties: dict[str, object] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["test", "cian", "yandex_realty"] = "test"
    polygon: PolygonFeature
    property_types: list[Literal["studio", "one_room"]] = Field(min_length=1)
    min_area: float | None = Field(default=None, ge=0)
    max_area: float | None = Field(default=None, ge=0)
    max_price: int = Field(gt=0)

    @field_validator("property_types")
    @classmethod
    def validate_property_types(
        cls, property_types: list[Literal["studio", "one_room"]]
    ) -> list[Literal["studio", "one_room"]]:
        if len(set(property_types)) != len(property_types):
            raise ValueError("Property types must not repeat.")
        return property_types

    @model_validator(mode="after")
    def validate_area_range(self) -> "SearchRequest":
        if (
            self.min_area is not None
            and self.max_area is not None
            and self.min_area > self.max_area
        ):
            raise ValueError("Minimum area cannot exceed maximum area.")
        return self


class SearchResponse(BaseModel):
    status: SourceStatus
    source: Literal["test", "cian", "yandex_realty"]
    total_before_filtering: int
    found_count: int
    listings: list[Listing]
    error: str | None = None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="index.html",
        request=request,
        context={
            "request": request,
            "default_max_price": 90000,
        },
    )


@app.post("/api/search", response_model=SearchResponse)
async def create_search(search_request: SearchRequest) -> SearchResponse:
    params = SourceSearchParams(
        property_types=search_request.property_types,
        min_area=search_request.min_area,
        max_area=search_request.max_area,
        max_price=search_request.max_price,
        polygon=search_request.polygon.geometry.coordinates[0],
    )
    source_result = await SOURCES[search_request.source].search(params)

    if source_result.status == "ok" and source_result.listings:
        app.state.last_search_for_export = ExportSearch(
            source=search_request.source,
            property_types=tuple(search_request.property_types),
            min_area=search_request.min_area,
            max_area=search_request.max_area,
            max_price=search_request.max_price,
            listings=tuple(source_result.listings),
        )
    else:
        app.state.last_search_for_export = None

    return SearchResponse(
        status=source_result.status,
        source=search_request.source,
        total_before_filtering=source_result.total_before_filtering,
        found_count=len(source_result.listings),
        listings=source_result.listings,
        error=source_result.error,
    )


@app.get("/api/export/excel")
async def export_excel(include_unverified: bool = True) -> FileResponse:
    try:
        export_path = create_excel_export(
            app.state.last_search_for_export,
            OUTPUT_DIR,
            include_unverified,
        )
    except NoExportResultsError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return FileResponse(
        export_path,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=export_path.name,
    )


@app.get("/api/export/html")
async def export_html(include_unverified: bool = True) -> FileResponse:
    try:
        export_path = create_html_export(
            app.state.last_search_for_export,
            OUTPUT_DIR,
            include_unverified,
        )
    except NoExportResultsError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return FileResponse(
        export_path,
        media_type="text/html; charset=utf-8",
        filename=export_path.name,
    )
