import unittest
from pathlib import Path

from nixloom.config import Config, ConfigError, RuntimePaths
from nixloom.operations import _choice_text, selected_assets

ROOT = Path(__file__).resolve().parents[1]


class OperationTests(unittest.TestCase):
    def test_choice_text_requires_assistant_content(self) -> None:
        response = {"choices": [{"message": {"content": "  OK  "}}]}
        self.assertEqual(_choice_text(response, "chat"), "OK")
        with self.assertRaises(ConfigError):
            _choice_text({"choices": []}, "chat")

    def test_default_model_selection_omits_disabled_video_assets(self) -> None:
        config = Config.load(RuntimePaths.from_environment(str(ROOT / "config.yaml")))
        names = {name for name, _ in selected_assets(config, [])}
        self.assertEqual(
            names,
            {
                "qwen36_q4",
                "qwen36_mmproj",
                "z_image_turbo_int8_convrot",
                "qwen_3_4b_fp8_mixed",
                "ae",
                "flux-2-klein-4b-nvfp4",
                "qwen_3_4b_fp4_flux2",
                "flux2-vae",
            },
        )

    def test_default_model_selection_adds_video_assets_when_enabled(self) -> None:
        config = Config.load(RuntimePaths.from_environment(str(ROOT / "config.yaml")))
        config.value["video"]["enabled"] = True
        config.validate()
        names = {name for name, _ in selected_assets(config, [])}
        self.assertIn("minimax_h3_nvfp4", names)
        self.assertIn("qwen3vl_32b_minimax_h3_nvfp4_awq", names)
        self.assertIn("minimax_h3_video_vae_fp16", names)
        self.assertIn("minimax_h3_audio_vae_fp32", names)

    def test_explicit_asset_selection_can_download_disabled_video_model(self) -> None:
        config = Config.load(RuntimePaths.from_environment(str(ROOT / "config.yaml")))
        selected = selected_assets(config, ["minimax_h3_nvfp4"])
        self.assertEqual([name for name, _ in selected], ["minimax_h3_nvfp4"])


if __name__ == "__main__":
    unittest.main()
