"""Stateful CLI operations: assets, backups, and live regression probes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, RuntimePaths


def human_size(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or suffix == "TiB":
            return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_asset(path: Path, size: int, digest: str) -> bool:
    return path.is_file() and path.stat().st_size == size and _sha256(path) == digest


def selected_assets(config: Config, requested: Iterable[str]) -> list[tuple[str, dict[str, Any]]]:
    catalog = config.get("assets", {})
    assert isinstance(catalog, dict)
    names = list(requested) or sorted(catalog)
    unknown = [name for name in names if name not in catalog]
    if unknown:
        raise ConfigError("unknown assets: " + ", ".join(unknown))
    return [(name, catalog[name]) for name in names]


def check_models(config: Config, paths: RuntimePaths, requested: Iterable[str]) -> bool:
    passed = True
    for name, asset in selected_assets(config, requested):
        target = paths.data / asset["path"]
        if verify_asset(target, asset["size"], asset["sha256"]):
            print(f"verified {name:<20} {asset['path']}")
        else:
            print(f"missing or invalid {name:<10} {asset['path']}", file=os.sys.stderr)
            passed = False
    return passed


def _download(url: str, target: Path, token: str) -> None:
    headers: dict[str, str] = {}
    if token and url.startswith("https://civitai.com/"):
        headers["Authorization"] = f"Bearer {token}"
    offset = target.stat().st_size if target.exists() else 0
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        append = offset > 0 and response.status == 206
        with target.open("ab" if append else "wb") as output:
            shutil.copyfileobj(response, output, length=8 * 1024 * 1024)


def download_models(config: Config, paths: RuntimePaths, requested: Iterable[str]) -> bool:
    token = config.string("credentials.civitai_api_token", "")
    passed = True
    for name, asset in selected_assets(config, requested):
        target = paths.data / asset["path"]
        if verify_asset(target, asset["size"], asset["sha256"]):
            print(f"verified {name:<20} {asset['path']}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        if partial.exists() and partial.stat().st_size >= asset["size"]:
            partial.unlink()
        print(f"downloading {name:<17} {asset['path']} ({human_size(asset['size'])})")
        try:
            _download(asset["url"], partial, token)
            if not verify_asset(partial, asset["size"], asset["sha256"]):
                print(f"retrying {name} from the beginning after verification failure", file=os.sys.stderr)
                partial.unlink(missing_ok=True)
                _download(asset["url"], partial, token)
            if not verify_asset(partial, asset["size"], asset["sha256"]):
                raise ConfigError(f"asset failed size/SHA-256 verification: {name}")
            os.replace(partial, target)
            print(f"verified {name:<20} {asset['path']}")
        except (OSError, urllib.error.URLError, ConfigError) as error:
            partial.unlink(missing_ok=True)
            print(f"download failed for {name}: {error}", file=os.sys.stderr)
            passed = False
    return passed


def _snapshot_sqlite(source: Path, target: Path) -> None:
    target.unlink(missing_ok=True)
    try:
        with (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30) as original,
            sqlite3.connect(target) as snapshot,
        ):
            original.backup(snapshot)
    except sqlite3.Error:
        shutil.copy2(source, target)


def create_backup(config: Config, paths: RuntimePaths, destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    with tempfile.TemporaryDirectory(prefix="nixloom-backup-") as temporary:
        stage = Path(temporary)
        shutil.copy2(paths.config_file, stage / "config.yaml")
        staged_state = stage / "state"
        staged_state.mkdir()
        for relative in (Path(".openclaw"), Path(".sillytavern/xdg-data")):
            source = paths.state / relative
            if source.exists():
                shutil.copytree(source, staged_state / relative, symlinks=True)
        for database in staged_state.rglob("*.db"):
            source = paths.state / database.relative_to(staged_state)
            if source.is_file():
                _snapshot_sqlite(source, database)
        for sidecar in list(staged_state.rglob("*.db-wal")) + list(staged_state.rglob("*.db-shm")):
            sidecar.unlink(missing_ok=True)

        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        archive = destination / f"nixloom-{stamp}.tar.gz"
        temporary_archive = archive.with_name(archive.name + ".tmp")
        with tarfile.open(temporary_archive, "w:gz") as output:
            output.add(stage / "config.yaml", arcname="config.yaml", recursive=False)
            if any(staged_state.iterdir()):
                output.add(staged_state, arcname="state")
        with tarfile.open(temporary_archive, "r:gz") as verification:
            if "config.yaml" not in verification.getnames():
                raise ConfigError("backup verification failed: config.yaml is absent")
        os.chmod(temporary_archive, 0o600)
        os.replace(temporary_archive, archive)
    return archive


def _json_request(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer nixloom-local"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ConfigError(f"unexpected response from {url}")
    return result


def _png_data_url() -> str:
    width = height = 32
    raw = b"".join(b"\x00" + b"\x33\x99\xff" * width for _ in range(height))

    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def live_test(config: Config, *, skip_image: bool = False) -> None:
    port = config.integer("ports.llama", minimum=1)
    base = f"http://127.0.0.1:{port}"
    model = config.string("llm.id")
    cases = [
        (
            "chat",
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "max_tokens": 16,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        ),
        (
            "reasoning",
            {
                "model": model,
                "messages": [{"role": "user", "content": "What is 17 + 25? Give only the number."}],
                "max_tokens": 128,
                "chat_template_kwargs": {"enable_thinking": True},
            },
        ),
        (
            "vision",
            {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What is the dominant color? One word."},
                            {"type": "image_url", "image_url": {"url": _png_data_url()}},
                        ],
                    }
                ],
                "max_tokens": 32,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        ),
    ]
    for label, payload in cases:
        response = _json_request(f"{base}/v1/chat/completions", payload, 900)
        if not response.get("choices"):
            raise ConfigError(f"{label} regression returned no choices")
        print(f"ok  {label}")
    if config.boolean("images.enabled") and not skip_image:
        _, profile = config.image_profile()
        prompt = "simple blue circle on a white background, test image"
        response = _json_request(
            f"{base}/upstream/sd/v1/images/generations",
            {
                "model": Path(str(profile["model_file"])).stem,
                "prompt": prompt,
                "size": "512x512",
                "n": 1,
                "response_format": "b64_json",
            },
            7200,
        )
        if not response.get("data"):
            raise ConfigError("OpenAI Images regression returned no image")
        print("ok  image (OpenAI Images API)")
        response = _json_request(f"{base}/v1/chat/completions", cases[0][1], 900)
        if not response.get("choices"):
            raise ConfigError("LLM did not recover after the image swap")
        print("ok  swap-back")


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        check=check,
        text=True,
        capture_output=not check,
    )
