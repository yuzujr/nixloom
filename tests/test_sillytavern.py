import json
import tempfile
import unittest
from pathlib import Path

from nixloom.config import Config, RuntimePaths
from nixloom.sillytavern import sync_settings

ROOT = Path(__file__).resolve().parents[1]


class SillyTavernTests(unittest.TestCase):
    def test_sync_uses_sdcpp_and_preserves_unmanaged_state(self) -> None:
        paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        config = Config.load(paths)
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "unmanaged": {"keep": True},
                        "oai_settings": {},
                        "extension_settings": {"sd": {}},
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(sync_settings(config, settings))
            updated = json.loads(settings.read_text(encoding="utf-8"))
            self.assertTrue(updated["unmanaged"]["keep"])
            image = updated["extension_settings"]["sd"]
            self.assertEqual(image["source"], "sdcpp")
            self.assertTrue(image["sdcpp_url"].endswith("/upstream/sd"))
            self.assertIn("masterpiece", image["prompt_prefix"])


if __name__ == "__main__":
    unittest.main()
