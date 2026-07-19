from pathlib import Path
import subprocess
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RepositoryPrivacyTests(unittest.TestCase):
    @staticmethod
    def is_ignored(relative_path: str) -> bool:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", relative_path],
            cwd=PROJECT_ROOT,
            check=False,
        )
        return result.returncode == 0

    def test_local_runtime_files_are_ignored(self) -> None:
        legacy_marker = "m" + "om"
        private_paths = (
            f"config/{legacy_marker}_search.json",
            f"config/cian_{legacy_marker}_search.json",
            "data/browser_profile",
            f"output/{legacy_marker}/index.html",
            f"debug/{legacy_marker}_search_last_run.json",
        )
        self.assertTrue(all(self.is_ignored(path) for path in private_paths))

    def test_public_examples_are_not_ignored(self) -> None:
        self.assertFalse(self.is_ignored("config/search_preset.example.json"))
        self.assertFalse(self.is_ignored("config/source_search.example.json"))

    def test_public_candidates_do_not_contain_private_terms(self) -> None:
        candidates = (
            "README.md",
            "app/services/preset_runner.py",
            "app/services/saved_search.py",
            "app/services/source_search.py",
            "app/services/browser_source_runner.py",
            "config/search_preset.example.json",
            "config/source_search.example.json",
            "Run Saved Search.cmd",
        )
        prohibited = (
            "m" + "om",
            "m" + "other",
            "м" + "ама",
            "мам" + "ы",
        )

        for relative_path in candidates:
            contents = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertFalse(
                    any(term in contents.casefold() for term in prohibited)
                )
