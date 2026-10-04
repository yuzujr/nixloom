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
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import comfy
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


def selected_assets(
    config: Config, requested: Iterable[str]
) -> list[tuple[str, dict[str, Any]]]:
    catalog = config.get("assets", {})
    assert isinstance(catalog, dict)
    names = list(requested)
    if not names:
        paths = {
            config.string("llm.model_file"),
            config.string("llm.mmproj_file"),
        }
        if config.boolean("images.enabled", False):
            profiles = config.get("images.profiles", {})
            for task in ("generate", "edit"):
                profile = profiles[config.string(f"images.{task}")]
                paths.update(
                    f"comfyui/models/{directory}/{profile[field]}"
                    for field, directory in (
                        ("model_file", "diffusion_models"),
                        ("text_encoder", "text_encoders"),
                        ("vae", "vae"),
                    )
                )
        if config.boolean("video.enabled", False):
            paths.update(
                f"comfyui/models/{directory}/{config.string(f'video.{field}')}"
                for field, directory in (
                    ("model_file", "diffusion_models"),
                    ("text_encoder", "text_encoders"),
                    ("video_vae", "vae"),
                    ("audio_vae", "vae"),
                )
            )
        names = [
            name for name, asset in catalog.items() if asset.get("path") in paths
        ]
    unknown = [name for name in names if name not in catalog]
    if unknown:
        raise ConfigError("unknown assets: " + ", ".join(unknown))
    return [(name, catalog[name]) for name in names]


def check_models(config: Config, paths: RuntimePaths, requested: Iterable[str]) -> bool:
    passed = True
    for name, asset in selected_assets(config, requested):
        target = paths.data / asset["path"]
        print(f"checking {name:<20} {asset['path']}", flush=True)
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


def download_models(
    config: Config, paths: RuntimePaths, requested: Iterable[str]
) -> bool:
    token = config.string("credentials.civitai_api_token", "")
    passed = True
    for name, asset in selected_assets(config, requested):
        target = paths.data / asset["path"]
        print(f"checking {name:<20} {asset['path']}", flush=True)
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
                print(
                    f"retrying {name} from the beginning after verification failure",
                    file=os.sys.stderr,
                )
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
        for relative in (
            Path(".dsh"),
            Path(".sillytavern/xdg-data"),
        ):
            source = paths.state / relative
            if source.exists():
                shutil.copytree(
                    source,
                    staged_state / relative,
                    symlinks=True,
                    ignore=shutil.ignore_patterns("node_modules")
                    if relative == Path(".dsh")
                    else None,
                )
        for database in staged_state.rglob("*.db"):
            source = paths.state / database.relative_to(staged_state)
            if source.is_file():
                _snapshot_sqlite(source, database)
        for sidecar in list(staged_state.rglob("*.db-wal")) + list(
            staged_state.rglob("*.db-shm")
        ):
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
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer nixloom-local",
        },
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
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    png = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )
    png += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _choice_text(response: dict[str, Any], label: str) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ConfigError(f"{label} regression returned no choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ConfigError(f"{label} regression returned no text")
    return content.strip()


def _comfy_image(config: Config, name: str, prompt: str, *, image: str = "") -> bytes:
    graph = comfy.workflows(config)[name]
    document = json.dumps(graph).replace("{{prompt}}", "__NIXLOOM_PROMPT__")
    graph = json.loads(document)
    for node in graph.values():
        for key, value in node["inputs"].items():
            if value == "__NIXLOOM_PROMPT__":
                node["inputs"][key] = prompt
            elif value == "{{seed}}":
                node["inputs"][key] = 42
            elif value == "{{image}}":
                node["inputs"][key] = image
    base = f"http://127.0.0.1:{config.integer('ports.comfyui', 8188, minimum=1)}"
    response = _json_request(base + "/prompt", {"prompt": graph}, 900)
    prompt_id = response.get("prompt_id")
    if not prompt_id:
        raise ConfigError(f"ComfyUI rejected workflow: {response}")
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        with urllib.request.urlopen(
            base + "/history/" + prompt_id, timeout=30
        ) as response:
            history = json.load(response).get(prompt_id)
        if history:
            if history.get("status", {}).get("status_str") != "success":
                raise ConfigError(f"ComfyUI execution failed: {history.get('status')}")
            images = history.get("outputs", {}).get("10", {}).get("images", [])
            if not images:
                raise ConfigError("ComfyUI returned no output image")
            with urllib.request.urlopen(
                base + "/view?" + urllib.parse.urlencode(images[0]), timeout=30
            ) as response:
                return response.read()
        time.sleep(0.5)
    raise ConfigError("ComfyUI generation timed out")


def _comfy_upload(config: Config, data: bytes) -> str:
    boundary = "nixloom-regression-upload"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="regression.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    url = f"http://127.0.0.1:{config.integer('ports.comfyui', 8188, minimum=1)}/upload/image"
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    return "/".join(part for part in (value.get("subfolder"), value["name"]) if part)


def live_test(
    config: Config,
    paths: RuntimePaths,
    *,
    skip_image: bool = False,
) -> None:
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
                "messages": [
                    {
                        "role": "user",
                        "content": "What is 17 + 25? Give only the number.",
                    }
                ],
                # Leave room for the reasoning trace and the final answer;
                # llama.cpp accounts both against this request budget.
                "max_tokens": 512,
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
                            {
                                "type": "text",
                                "text": "What is the dominant color? One word.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": _png_data_url()},
                            },
                        ],
                    }
                ],
                "max_tokens": 32,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        ),
    ]
    expectations = {"chat": "OK", "reasoning": "42", "vision": "blue"}
    for label, payload in cases:
        response = _json_request(f"{base}/v1/chat/completions", payload, 900)
        content = _choice_text(response, label)
        expected = expectations[label]
        if label == "chat" and content != expected:
            raise ConfigError(f"chat regression expected 'OK', got {content!r}")
        if label != "chat" and expected not in content.lower():
            raise ConfigError(
                f"{label} regression expected {expected!r}, got {content!r}"
            )
        print(f"ok  {label}")
    if config.boolean("images.enabled") and not skip_image:
        image = _comfy_image(
            config,
            config.string("images.generate"),
            "A realistic photograph of a woman in a green jacket sitting at a coastal cafe, natural light.",
        )
        print("ok  image (Z-Image / ComfyUI)")
        reference = _comfy_upload(config, image)
        _comfy_image(
            config,
            config.string("images.edit"),
            "Change the jacket to red. Preserve the person's face, pose, and background.",
            image=reference,
        )
        print("ok  edit (Klein / ComfyUI)")
        response = _json_request(f"{base}/v1/chat/completions", cases[0][1], 900)
        if _choice_text(response, "swap-back") != "OK":
            raise ConfigError(
                "LLM did not return the expected response after the image swap"
            )
        print("ok  swap-back")


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        check=check,
        text=True,
        capture_output=not check,
    )
