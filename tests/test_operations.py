import unittest

from nixloom.config import ConfigError
from nixloom.operations import _choice_text


class OperationTests(unittest.TestCase):
    def test_choice_text_requires_assistant_content(self) -> None:
        response = {"choices": [{"message": {"content": "  OK  "}}]}
        self.assertEqual(_choice_text(response, "chat"), "OK")
        with self.assertRaises(ConfigError):
            _choice_text({"choices": []}, "chat")


if __name__ == "__main__":
    unittest.main()
