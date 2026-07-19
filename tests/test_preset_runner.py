import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.models import Listing
from app.services.preset_runner import main, run_saved_search
from app.services.saved_search import (
    DEFAULT_SAVED_SEARCH_CONFIG,
    load_saved_search_config,
    save_saved_search_config,
)
from app.sources.base import SourceSearchResult


class FakeSource:
    def __init__(self, result: SourceSearchResult) -> None:
        self.result = result
        self.calls = 0

    async def search(self, _params: object) -> SourceSearchResult:
        self.calls += 1
        return self.result


def example_listing() -> Listing:
    return Listing(
        source="cian",
        external_id="example-1",
        url="https://example.com/listings/example-1",
        title="Example one-room apartment",
        property_type="one_room",
        rent_price=70000,
        area_sqm=35,
        metro_station="Example Station A",
        metro_minutes=10,
        address="Example Street, 1",
        latitude=None,
        longitude=None,
        location_verified=False,
    )


class SavedSearchConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config_path = Path(self.temporary_directory.name) / "search_preset.json"

    def test_default_configuration_uses_only_real_sources(self) -> None:
        config = load_saved_search_config(self.config_path)

        self.assertEqual(config["enabled_sources"], ["cian", "yandex_realty"])
        self.assertNotIn("test", config["enabled_sources"])
        self.assertEqual(config, DEFAULT_SAVED_SEARCH_CONFIG)

    def test_save_is_atomic_and_keeps_generic_output_path(self) -> None:
        preset = {
            "polygon": None,
            "property_types": ["studio", "one_room"],
            "min_area": 20,
            "max_area": 50,
            "max_price": 90000,
            "include_unverified_locations": True,
        }

        saved = save_saved_search_config(self.config_path, preset)

        self.assertEqual(saved["output_directory"], "output/saved-search")
        self.assertEqual(saved["enabled_sources"], ["cian", "yandex_realty"])
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8")), saved
        )
        self.assertEqual(list(self.config_path.parent.glob("*.tmp")), [])


class PresetRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.project_root = Path(self.temporary_directory.name)
        self.config_path = self.project_root / "config" / "search_preset.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "polygon": None,
                    "property_types": ["studio", "one_room"],
                    "min_area": 20,
                    "max_area": 50,
                    "max_price": 90000,
                    "include_unverified_locations": True,
                    "location_filter_mode": "metro",
                    "allowed_metro_stations": ["Example Station A"],
                    "max_metro_minutes": 20,
                    "enabled_sources": ["cian", "yandex_realty"],
                    "output_directory": "output/saved-search",
                }
            ),
            encoding="utf-8",
        )

    def test_generic_runner_creates_a_neutral_report_with_mocked_sources(self) -> None:
        cian = FakeSource(
            SourceSearchResult(
                status="ok",
                total_before_filtering=1,
                listings=[example_listing()],
            )
        )
        yandex = FakeSource(
            SourceSearchResult(status="ok", total_before_filtering=0, listings=[])
        )

        search_run = asyncio.run(
            run_saved_search(
                config_path=self.config_path,
                project_root=self.project_root,
                source_adapters={"cian": cian, "yandex_realty": yandex},
            )
        )
        report = search_run.output_path.read_text(encoding="utf-8")

        self.assertEqual(cian.calls, 1)
        self.assertEqual(yandex.calls, 1)
        self.assertEqual(
            search_run.output_path,
            self.project_root / "output" / "saved-search" / "index.html",
        )
        self.assertIn("Rental Search Report", report)
        self.assertIn("https://example.com/listings/example-1", report)

    def test_cli_explains_missing_generic_configuration(self) -> None:
        messages: list[str] = []

        with patch("builtins.print", side_effect=messages.append):
            status = main(["--config", str(self.project_root / "missing.json")])

        self.assertEqual(status, 1)
        self.assertEqual(
            messages,
            [
                "Copy config/search_preset.example.json to "
                "config/search_preset.json and configure your search."
            ],
        )
