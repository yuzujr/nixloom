"""Install and verify the one audited patch required by managed DSH access."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import ConfigError

RESOURCES = Path(__file__).with_name("dsh-patches")
CONNECTION = Path("node_modules/@deepseek-ai/dsh-client-connection/lib/index.js")
ACCESS = CONNECTION.with_name("nixloom-access.mjs")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected() -> dict[str, str]:
    manifest = json.loads((RESOURCES / "manifest.json").read_text())
    return {
        "connection": manifest["patched"],
        "access": _digest(Path(__file__).with_name("dsh_access.mjs")),
        "patch": _digest(RESOURCES / "managed-access.patch"),
    }


def _restore_legacy(target: Path) -> None:
    modules = target / "node_modules/@deepseek-ai"
    terminal = modules / "dsh-terminal-bash/lib/index.js"
    source = terminal.read_text()
    restored = re.sub(
        r'const DEFAULT_BASH_SHELL = "(?:[^"\\]|\\.)*";',
        'const DEFAULT_BASH_SHELL = "/bin/bash";',
        source,
    )
    if restored != source:
        terminal.write_text(restored)
    controller = modules / "dsh-api-workspace-controller/lib/index.js"
    source = controller.read_text()
    restored = re.sub(r"\n/\* NixLoom default workspace \*/return [^\n]+;", "", source)
    if restored != source:
        controller.write_text(restored)
    connection = target / CONNECTION
    source = connection.read_text()
    if "/* NixLoom loopback access */" in source:
        restored = re.sub(
            r"\n\t\t/\* NixLoom loopback access \*/\n\t\t[^\n]+", "", source
        ).replace(
            '\n\t\tif (process.env.NIXLOOM_DSH_LOCAL_ACCESS === "1") return baseUrl;',
            "",
        )
        connection.write_text(restored)


def install(target: Path) -> None:
    _restore_legacy(target)
    connection = target / CONNECTION
    manifest = json.loads((RESOURCES / "manifest.json").read_text())
    digest = _digest(connection)
    if digest not in {manifest["upstream"], manifest["patched"]}:
        raise ConfigError(
            "DSH connection source changed; review the managed-access patch"
        )
    if digest == manifest["upstream"]:
        with tempfile.TemporaryDirectory(prefix=".patch-", dir=target) as temporary:
            staging = Path(temporary)
            staged = staging / CONNECTION
            staged.parent.mkdir(parents=True)
            shutil.copyfile(connection, staged)
            subprocess.run(
                [
                    "patch",
                    "--batch",
                    "--fuzz=0",
                    "-p1",
                    "-i",
                    str(RESOURCES / "managed-access.patch"),
                ],
                cwd=staging,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            if _digest(staged) != manifest["patched"]:
                raise ConfigError(
                    "DSH managed-access patch did not produce the expected source"
                )
            staged.replace(connection)
    helper = Path(__file__).with_name("dsh_access.mjs")
    shutil.copyfile(helper, target / ACCESS)
    (target / ".nixloom-compat.json").write_text(json.dumps(_expected()))


def verify(target: Path) -> None:
    stamp = target / ".nixloom-compat.json"
    expected = _expected()
    if (
        not stamp.is_file()
        or json.loads(stamp.read_text()) != expected
        or not (target / ACCESS).is_file()
        or _digest(target / CONNECTION) != expected["connection"]
        or _digest(target / ACCESS) != expected["access"]
    ):
        raise ConfigError(
            "DSH compatibility files are stale; rerun Home Manager activation"
        )
