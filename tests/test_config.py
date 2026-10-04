import unittest
from pathlib import Path
from unittest.mock import patch

from nixloom.config import Config, ConfigError, RuntimePaths

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_packaged_template_is_valid(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        self.assertEqual(config.string("llm.id"), "qwen")

    def test_video_models_are_pinned_in_the_project_asset_catalog(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        assets = {
            asset["path"]: asset for asset in config.get("assets").values()
        }
        expected = {
            "model_file": "diffusion_models",
            "text_encoder": "text_encoders",
            "video_vae": "vae",
            "audio_vae": "vae",
        }
        for setting, directory in expected.items():
            filename = config.string(f"video.{setting}")
            path = f"comfyui/models/{directory}/{filename}"
            self.assertIn(path, assets)
            self.assertEqual(len(assets[path]["sha256"]), 64)

    def test_image_profile_models_are_pinned_in_the_project_asset_catalog(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        assets = {
            asset["path"]: asset for asset in config.get("assets").values()
        }
        for profile in config.get("images.profiles").values():
            directories = {
                "model_file": "diffusion_models",
                "text_encoder": "text_encoders",
                "vae": "vae",
            }
            for setting, directory in directories.items():
                path = f"comfyui/models/{directory}/{profile[setting]}"
                with self.subTest(path=path):
                    self.assertIn(path, assets)
                    self.assertEqual(len(assets[path]["sha256"]), 64)

    def test_output_limit_must_fit_context(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        config.value["llm"]["max_tokens"] = config.value["llm"]["context"]
        with self.assertRaises(ConfigError):
            config.validate()

    def test_disabled_images_need_no_profile_or_image_runtime(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        config.value["images"] = {"enabled": False}
        with patch.dict("os.environ", {"NIXLOOM_IMAGE_RUNTIME": "disabled"}):
            config.validate()

    def test_enabled_images_require_the_nix_runtime(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        with (
            patch.dict("os.environ", {"NIXLOOM_IMAGE_RUNTIME": "disabled"}),
            self.assertRaisesRegex(ConfigError, "services.nixloom.images.enable"),
        ):
            config.validate()

    def test_n_cpu_moe_validation(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        config.value["llm"]["n_cpu_moe"] = "invalid"
        with self.assertRaisesRegex(ConfigError, "n_cpu_moe"):
            config.validate()
        config.value["llm"]["n_cpu_moe"] = -1
        with self.assertRaisesRegex(ConfigError, "n_cpu_moe"):
            config.validate()
        config.value["llm"]["n_cpu_moe"] = 40
        config.validate()
        config.value["llm"]["n_cpu_moe"] = "auto"
        config.validate()


if __name__ == "__main__":
    unittest.main()
