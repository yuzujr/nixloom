import base64
import unittest

from nixloom.config import ConfigError
from nixloom.operations import _choice_text, _image_bytes


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


if __name__ == "__main__":
    unittest.main()
