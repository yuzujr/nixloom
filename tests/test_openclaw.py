import unittest
from pathlib import Path
from unittest.mock import patch

from nixloom.config import Config, RuntimePaths
from nixloom.openclaw import (
    IMAGE_SETTINGS,
    TAVILY_SETTINGS,
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
        self.assertTrue(settings["tools.loopDetection.enabled"])
        self.assertTrue(
            settings["tools.web.fetch.ssrfPolicy.allowRfc2544BenchmarkRange"]
        )

    def test_tavily_provider_is_configured_when_key_exists(self) -> None:
        self.config.value["credentials"]["tavily_api_key"] = "tvly-test"
        with (
            patch("nixloom.openclaw._sync_plugin_path") as sync_plugin_path,
            patch("nixloom.openclaw._set_batch") as set_batch,
            patch.dict(
                "os.environ",
                {"NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH": "/plugin/tavily"},
            ),
            patch.object(Path, "is_file", return_value=True),
        ):
            from nixloom.openclaw import _configure_tavily

            _configure_tavily(self.config)
        sync_plugin_path.assert_called_once_with(
            Path("/plugin/tavily"), "/share/nixloom/openclaw-plugin-tavily"
        )
        configured = {
            item["path"]: item["value"] for item in set_batch.call_args.args[0]
        }
        self.assertTrue(configured["plugins.entries.tavily.enabled"])
        self.assertEqual(configured["tools.web.search.provider"], "tavily")

    def test_tavily_provider_is_removed_when_key_is_absent(self) -> None:
        self.config.value["credentials"]["tavily_api_key"] = ""
        with (
            patch("nixloom.openclaw._sync_plugin_path") as sync_plugin_path,
            patch("nixloom.openclaw._unset_paths") as unset_paths,
        ):
            from nixloom.openclaw import _configure_tavily

            _configure_tavily(self.config)
        sync_plugin_path.assert_called_once_with(
            None, "/share/nixloom/openclaw-plugin-tavily"
        )
        unset_paths.assert_called_once_with(TAVILY_SETTINGS)

    def test_disabled_features_converge_to_absence(self) -> None:
        self.config.value["images"]["enabled"] = False
        self.config.value["openclaw"]["workspace"] = ""
        self.config.value["openclaw"]["yuanbao"] = False
        expected_unsets = ["agents.defaults.workspace", *IMAGE_SETTINGS]
        with (
            patch("nixloom.openclaw._set_batch") as set_batch,
            patch("nixloom.openclaw._unset_paths") as unset_paths,
            patch("nixloom.openclaw._disable_yuanbao") as disable_yuanbao,
            patch("nixloom.openclaw._configure_tavily") as configure_tavily,
        ):
            reconcile_settings(self.config)
        set_batch.assert_called_once()
        unset_paths.assert_called_once_with(expected_unsets)
        disable_yuanbao.assert_called_once_with()
        configure_tavily.assert_called_once_with(self.config)
        self.assertEqual(len(YUANBAO_SETTINGS), 4)


if __name__ == "__main__":
    unittest.main()
