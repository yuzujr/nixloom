import difflib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nixloom import dsh_compat
from nixloom.config import ConfigError


class CompatibilityTests(unittest.TestCase):
    def test_patch_is_verified_idempotent_and_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = root / "patches"
            resources.mkdir()
            target = root / "app"
            connection = target / dsh_compat.CONNECTION
            connection.parent.mkdir(parents=True)
            original, changed = "original\n", "changed\n"
            connection.write_text(original)
            for name in ("dsh-terminal-bash", "dsh-api-workspace-controller"):
                source = target / "node_modules/@deepseek-ai" / name / "lib/index.js"
                source.parent.mkdir(parents=True)
                source.write_text("unchanged\n")
            (resources / "manifest.json").write_text(
                json.dumps(
                    {
                        "upstream": hashlib.sha256(original.encode()).hexdigest(),
                        "patched": hashlib.sha256(changed.encode()).hexdigest(),
                    }
                )
            )
            (resources / "managed-access.patch").write_text(
                "".join(
                    difflib.unified_diff(
                        original.splitlines(keepends=True),
                        changed.splitlines(keepends=True),
                        fromfile="a/" + str(dsh_compat.CONNECTION),
                        tofile="b/" + str(dsh_compat.CONNECTION),
                    )
                )
            )
            with patch("nixloom.dsh_compat.RESOURCES", resources):
                dsh_compat.install(target)
                self.assertEqual(connection.read_text(), changed)
                dsh_compat.install(target)
                dsh_compat.verify(target)
                connection.write_text("tampered\n")
                with self.assertRaisesRegex(ConfigError, "activation"):
                    dsh_compat.verify(target)
                with self.assertRaisesRegex(ConfigError, "source changed"):
                    dsh_compat.install(target)
