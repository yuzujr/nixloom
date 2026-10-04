import unittest
from pathlib import Path

from nixloom.config import Config, RuntimePaths
from nixloom.runtime import llama_command, swap_document

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
        self.assertIn("--fit", command)
        self.assertNotIn("--n-cpu-moe", command)

    def test_llama_command_explicit_n_cpu_moe(self) -> None:
        self.config.value["llm"]["n_cpu_moe"] = 32
        try:
            command = llama_command(self.config, self.paths, port="${PORT}")
            self.assertIn("--n-cpu-moe", command)
            idx = command.index("--n-cpu-moe")
            self.assertEqual(command[idx + 1], "32")
        finally:
            self.config.value["llm"]["n_cpu_moe"] = "auto"

    def test_swap_manages_only_the_chat_model(self) -> None:
        document = swap_document(self.config, self.paths)
        self.assertEqual(set(document["models"]), {"qwen"})
        self.assertEqual(document["models"]["qwen"]["checkEndpoint"], "/health")


if __name__ == "__main__":
    unittest.main()
