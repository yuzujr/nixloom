import copy
import unittest
from pathlib import Path

from nixloom.comfy import command, workflows
from nixloom.config import Config, ConfigError, RuntimePaths

ROOT = Path(__file__).resolve().parents[1]


class ComfyTests(unittest.TestCase):
    def setUp(self):
        self.paths = RuntimePaths.from_environment(str(ROOT / "config.yaml"))
        self.config = Config.load(self.paths)

    def test_edit_uses_reference_conditioning_without_noising_the_reference(self):
        graph = workflows(self.config)["klein-edit"]
        self.assertEqual(graph["14"]["class_type"], "ReferenceLatent")
        self.assertEqual(graph["6"]["class_type"], "EmptyFlux2LatentImage")
        self.assertEqual(graph["11"]["inputs"]["image"], "{{image}}")
        self.assertEqual(graph["19"]["inputs"]["width"], ["16", 0])

    def test_comfy_backend_is_private_and_writable_paths_are_outside_store(self):
        launch = command(self.config, self.paths)
        self.assertEqual(launch[launch.index("--listen") + 1], "127.0.0.1")
        self.assertEqual(launch[launch.index("--port") + 1], "8189")
        for flag in ["--base-directory", "--user-directory", "--temp-directory"]:
            self.assertNotIn("/nix/store/", launch[launch.index(flag) + 1])

    def test_default_internal_port_collisions_are_rejected(self):
        self.config.value["ports"].pop("swap")
        self.config.value["ports"]["llama"] = 8187
        with self.assertRaisesRegex(ConfigError, "distinct"):
            self.config.validate()

    def test_model_paths_cannot_escape_managed_directory(self):
        self.config.value = copy.deepcopy(self.config.value)
        self.config.value["images"]["profiles"]["klein-edit"]["model_file"] = (
            "../private.safetensors"
        )
        with self.assertRaisesRegex(ConfigError, "relative"):
            self.config.validate()
