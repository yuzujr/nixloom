import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nixloom.config import ConfigError, RuntimePaths
from nixloom.operations import _choice_text, _image_bytes, _test_openclaw_agent


class OperationTests(unittest.TestCase):
    def test_choice_text_requires_assistant_content(self) -> None:
        response = {"choices": [{"message": {"content": "  OK  "}}]}
        self.assertEqual(_choice_text(response, "chat"), "OK")
        with self.assertRaises(ConfigError):
            _choice_text({"choices": []}, "chat")

    def test_image_response_must_contain_a_decodable_image(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"test"
        response = {"data": [{"b64_json": base64.b64encode(png).decode()}]}
        self.assertEqual(_image_bytes(response), png)
        with self.assertRaises(ConfigError):
            _image_bytes({"data": [{"b64_json": "not base64"}]})

    def test_openclaw_agent_reads_version_2026_6_33_json_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            config_path = state / ".openclaw/openclaw.json"
            token_path = state / ".run/openclaw-gateway-token"
            config_path.parent.mkdir()
            token_path.parent.mkdir()
            config_path.write_text("{}", encoding="utf-8")
            token_path.write_text("test-token", encoding="utf-8")
            paths = RuntimePaths(
                state=state,
                data=state / "data",
                cache=state / "cache",
                config_dir=state / "config",
                config_file=state / "config.yaml",
                share=None,
            )
            installed = type("Result", (), {"returncode": 0})()
            response = type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "result": {
                                "meta": {
                                    "finalAssistantVisibleText": "NIXLOOM_AGENT_TOOL_OK",
                                    "toolSummary": {
                                        "calls": 1,
                                        "tools": ["exec"],
                                        "failures": 0,
                                    },
                                }
                            }
                        }
                    ),
                },
            )()
            with patch(
                "nixloom.operations.subprocess.run", side_effect=[installed, response]
            ):
                _test_openclaw_agent(paths)


if __name__ == "__main__":
    unittest.main()
