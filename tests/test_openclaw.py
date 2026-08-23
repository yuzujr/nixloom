import unittest
from pathlib import Path
from unittest.mock import patch

from nixloom.config import Config, RuntimePaths
from nixloom.openclaw import (
    IMAGE_SETTINGS,
    YUANBAO_SETTINGS,
    managed_settings,
    reconcile_settings,
)

ROOT = Path(__file__).resolve().parents[1]


class OpenClawTests(unittest.TestCase):
    def setUp(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        self.config = Config.load(paths)

    def test_managed_provider_supports_reasoning_vision_and_images(self) -> None:
        settings = {
            item["path"]: item["value"] for item in managed_settings(self.config)
        }
        model = settings["models.providers.nixloom"]["models"][0]
        self.assertTrue(model["reasoning"])
        self.assertEqual(model["input"], ["text", "image"])
        self.assertIn("agents.defaults.imageGenerationModel", settings)

    def test_disabled_features_converge_to_absence(self) -> None:
        self.config.value["images"]["enabled"] = False
        self.config.value["openclaw"]["workspace"] = ""
        self.config.value["openclaw"]["yuanbao"] = False
        expected_unsets = ["agents.defaults.workspace", *IMAGE_SETTINGS]
        with (
            patch("nixloom.openclaw._set_batch") as set_batch,
            patch("nixloom.openclaw._unset_paths") as unset_paths,
            patch("nixloom.openclaw._disable_yuanbao") as disable_yuanbao,
        ):
            reconcile_settings(self.config)
        set_batch.assert_called_once()
        unset_paths.assert_called_once_with(expected_unsets)
        disable_yuanbao.assert_called_once_with()
        self.assertEqual(len(YUANBAO_SETTINGS), 4)


if __name__ == "__main__":
    unittest.main()
