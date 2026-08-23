import unittest
from pathlib import Path

from nixloom.config import Config, ConfigError, RuntimePaths

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_packaged_template_is_valid(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        self.assertEqual(config.string("llm.id"), "qwen")
        self.assertEqual(config.string("sillytavern.bind"), "0.0.0.0")

    def test_output_limit_must_fit_context(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        config.value["llm"]["max_tokens"] = config.value["llm"]["context"]
        with self.assertRaises(ConfigError):
            config.validate()


if __name__ == "__main__":
    unittest.main()
