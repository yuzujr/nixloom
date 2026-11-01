"""Install and verify patches required by managed DSH access and wildcard address binding."""

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
WEBSERVER = Path("node_modules/@deepseek-ai/dsh-host-webserver/lib/index.js")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected() -> dict[str, str]:
    manifest = json.loads((RESOURCES / "manifest.json").read_text())
    return {
        "connection": manifest["patched"],
        "access": _digest(Path(__file__).with_name("dsh_access.mjs")),
        "patch": _digest(RESOURCES / "managed-access.patch"),
        "webserver": manifest["webserver_patched"],
        "wildcard_patch": _digest(RESOURCES / "wildcard-host.patch"),
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


def _apply_patch(
    target: Path, relative_file: Path, patch_file: Path, expected_digest: str
) -> None:
    source_file = target / relative_file
    with tempfile.TemporaryDirectory(prefix=".patch-", dir=target) as temporary:
        staging = Path(temporary)
        staged = staging / relative_file
        staged.parent.mkdir(parents=True)
        shutil.copyfile(source_file, staged)
        subprocess.run(
            [
                "patch",
                "--batch",
                "--fuzz=0",
                "-p1",
                "-i",
                str(patch_file),
            ],
            cwd=staging,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if _digest(staged) != expected_digest:
            raise ConfigError(
                f"DSH patch {patch_file.name} did not produce the expected source"
            )
        staged.replace(source_file)


def install(target: Path) -> None:
    _restore_legacy(target)
    manifest = json.loads((RESOURCES / "manifest.json").read_text())

    connection = target / CONNECTION
    connection_digest = _digest(connection)
    if connection_digest not in {manifest["upstream"], manifest["patched"]}:
        raise ConfigError(
            "DSH connection source changed; review the managed-access patch"
        )
    if connection_digest == manifest["upstream"]:
        _apply_patch(
            target,
            CONNECTION,
            RESOURCES / "managed-access.patch",
            manifest["patched"],
        )

    webserver = target / WEBSERVER
    webserver_digest = _digest(webserver)
    if webserver_digest not in {
        manifest["webserver_upstream"],
        manifest["webserver_patched"],
    }:
        raise ConfigError(
            "DSH webserver source changed; review the wildcard-host patch"
        )
    if webserver_digest == manifest["webserver_upstream"]:
        _apply_patch(
            target,
            WEBSERVER,
            RESOURCES / "wildcard-host.patch",
            manifest["webserver_patched"],
        )

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
        or _digest(target / WEBSERVER) != expected["webserver"]
    ):
        raise ConfigError(
            "DSH compatibility files are stale; rerun Home Manager activation"
        )
