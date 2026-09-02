import unittest
from pathlib import Path

from nixloom.config import Config, ConfigError, RuntimePaths
from nixloom.runtime import image_command, llama_command, swap_document

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        cls.config = Config.load(cls.paths)

    def test_llama_command_preserves_reasoning_and_vision(self) -> None:
        command = llama_command(self.config, self.paths, port="${PORT}")
        self.assertEqual(command[0], "llama-server")
        self.assertIn("--mmproj", command)
        self.assertIn("--reasoning-preserve", command)

    def test_image_runtime_is_stable_diffusion_cpp(self) -> None:
        command = image_command(self.config, self.paths, port="${PORT}")
        self.assertEqual(command[0], "sd-server")
        self.assertIn("--lora-model-dir", command)
        self.assertIn("--prompt", command)
        self.assertIn("--vae-tiling", command)

    def test_swap_exposes_one_chat_model_and_hidden_image_runtime(self) -> None:
        document = swap_document(self.config, self.paths)
        self.assertEqual(set(document["models"]), {"qwen", "sd"})
        self.assertTrue(document["models"]["sd"]["unlisted"])
        self.assertEqual(document["models"]["sd"]["checkEndpoint"], "/v1/models")

    def test_disabled_image_runtime_is_absent(self) -> None:
        self.config.value["images"]["enabled"] = False
        try:
            document = swap_document(self.config, self.paths)
            self.assertEqual(set(document["models"]), {"qwen"})
            with self.assertRaisesRegex(ConfigError, "disabled"):
                image_command(self.config, self.paths)
        finally:
            self.config.value["images"]["enabled"] = True


if __name__ == "__main__":
    unittest.main()
