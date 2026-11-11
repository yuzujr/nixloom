"""Stateful CLI operations: assets, backups, and runtime operations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
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
        names = [name for name, asset in catalog.items() if asset.get("path") in paths]
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


def list_models(config: Config, paths: RuntimePaths, *, all_assets: bool = False) -> None:
    catalog = config.get("assets", {})
    assert isinstance(catalog, dict)
    active_pairs = selected_assets(config, [])
    active_names = {name for name, _ in active_pairs}

    items = catalog.items() if all_assets else [(k, catalog[k]) for k in active_names if k in catalog]
    if not items:
        print("no models configured.")
        return

    name_w = max(len("ASSET"), *(len(k) for k, _ in items)) + 2
    print(f"{'ASSET':<{name_w}} {'STATUS':<10} {'SIZE':<10} TARGET PATH")
    print("-" * (name_w + 50))
    for name, asset in items:
        target = paths.data / asset["path"]
        if target.is_file():
            status = "present" if target.stat().st_size == asset["size"] else "corrupted"
        else:
            status = "missing"
        active_prefix = "* " if (all_assets and name in active_names) else "  "
        print(f"{active_prefix}{name:<{name_w - 2}} {status:<10} {human_size(asset['size']):<10} {asset['path']}")

    if all_assets:
        print("\n(*) Assets required by current configuration.")


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
        try:
            shutil.copy2(source, target)
        except FileNotFoundError:
            target.unlink(missing_ok=True)


def _copytree_snapshot(
    source: Path, target: Path, *, skip_node_modules: bool = False
) -> None:
    """Copy a changing tree, ignoring entries that vanish during the copy."""
    try:
        source.lstat()
    except FileNotFoundError:
        return
    if not source.is_dir() or source.is_symlink():
        return
    target.mkdir(parents=True, exist_ok=True)
    try:
        with os.scandir(source) as directory:
            entries = list(directory)
    except FileNotFoundError:
        shutil.rmtree(target, ignore_errors=True)
        return

    for entry in entries:
        if skip_node_modules and entry.name == "node_modules":
            continue
        source_entry = source / entry.name
        target_entry = target / entry.name
        try:
            if entry.is_symlink():
                os.symlink(os.readlink(source_entry), target_entry)
            elif entry.is_dir(follow_symlinks=False):
                _copytree_snapshot(
                    source_entry,
                    target_entry,
                    skip_node_modules=skip_node_modules,
                )
            elif entry.is_file(follow_symlinks=False):
                shutil.copy2(source_entry, target_entry, follow_symlinks=False)
        except FileNotFoundError:
            if target_entry.is_symlink() or target_entry.is_file():
                target_entry.unlink(missing_ok=True)
            elif target_entry.is_dir():
                shutil.rmtree(target_entry, ignore_errors=True)
    try:
        shutil.copystat(source, target, follow_symlinks=False)
    except FileNotFoundError:
        pass


def create_backup(config: Config, paths: RuntimePaths, destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    with tempfile.TemporaryDirectory(prefix="nixloom-backup-") as temporary:
        stage = Path(temporary)
        shutil.copy2(paths.config_file, stage / "config.yaml")
        staged_state = stage / "state"
        staged_data = stage / "data"
        staged_state.mkdir()
        staged_data.mkdir()
        custom_workspace = config.string("dsh.workspace", "")
        data_relatives = [
            Path("comfyui/input"),
            Path("comfyui/output"),
            Path("comfyui/workflows"),
        ]
        if not custom_workspace:
            data_relatives.append(Path("dsh/workspace"))
        for root, relatives in (
            (paths.state, (Path(".dsh"), Path(".sillytavern"), Path("comfyui"))),
            (paths.data, tuple(data_relatives)),
        ):
            for relative in relatives:
                source = root / relative
                if not source.exists():
                    continue
                target_root = staged_state if root == paths.state else staged_data
                _copytree_snapshot(
                    source,
                    target_root / relative,
                    skip_node_modules=relative == Path(".dsh"),
                )
        if custom_workspace:
            source = Path(custom_workspace).expanduser()
            if source.exists():
                _copytree_snapshot(source, staged_data / "dsh/workspace")
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
            if any(staged_data.iterdir()):
                output.add(staged_data, arcname="data")
        with tarfile.open(temporary_archive, "r:gz") as verification:
            names = set(verification.getnames())
            required = {"config.yaml"}
            for path in stage.rglob("*"):
                if path.is_file():
                    required.add(path.relative_to(stage).as_posix())
            missing = required - names
            if missing:
                raise ConfigError(
                    "backup verification failed; missing: " + ", ".join(sorted(missing))
                )
        os.chmod(temporary_archive, 0o600)
        os.replace(temporary_archive, archive)
    return archive


def restore_backup(
    archive: Path, config: Config, paths: RuntimePaths, *, dry_run: bool = False
) -> tuple[int, int]:
    """Merge a NixLoom archive into the active state and data directories."""
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise ConfigError(f"backup archive not found: {archive}")

    with tempfile.TemporaryDirectory(prefix="nixloom-restore-") as temporary:
        stage = Path(temporary)
        files: list[tuple[tarfile.TarInfo, Path]] = []
        total_bytes = 0
        try:
            with tarfile.open(archive, "r:gz") as source:
                for member in source.getmembers():
                    name = Path(member.name)
                    if name.is_absolute() or ".." in name.parts:
                        raise ConfigError("backup contains an unsafe path")
                    if member.name == "config.yaml":
                        if not member.isfile():
                            raise ConfigError(
                                "backup config.yaml is not a regular file"
                            )
                        continue
                    if not name.parts or name.parts[0] not in {"state", "data"}:
                        raise ConfigError(f"unexpected backup entry: {member.name}")
                    relative_parts = name.parts[1:]
                    allowed_roots = (
                        ((".dsh",), (".sillytavern",), ("comfyui",))
                        if name.parts[0] == "state"
                        else (
                            ("dsh", "workspace"),
                            ("comfyui", "input"),
                            ("comfyui", "output"),
                            ("comfyui", "workflows"),
                        )
                    )
                    allowed = any(
                        relative_parts[: len(prefix)] == prefix
                        or (
                            member.isdir()
                            and prefix[: len(relative_parts)] == relative_parts
                        )
                        for prefix in allowed_roots
                    )
                    if not allowed:
                        raise ConfigError(f"unexpected backup entry: {member.name}")
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ConfigError(
                            f"backup contains a non-regular file: {member.name}"
                        )
                    relative = Path(*name.parts[1:])
                    if not relative.parts:
                        raise ConfigError(f"unexpected backup entry: {member.name}")
                    extracted = stage / name
                    extracted.parent.mkdir(parents=True, exist_ok=True)
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ConfigError(f"cannot read backup entry: {member.name}")
                    with stream, extracted.open("wb") as output:
                        shutil.copyfileobj(stream, output)
                    os.chmod(extracted, member.mode & 0o777)
                    os.utime(extracted, (member.mtime, member.mtime))
                    files.append((member, extracted))
                    total_bytes += member.size
        except (tarfile.TarError, OSError) as error:
            raise ConfigError(f"cannot read backup archive: {error}") from error

        if not files:
            raise ConfigError("backup contains no restorable data files")
        if dry_run:
            return len(files), total_bytes

        if config.string("dsh.workspace", ""):
            workspace = Path(config.string("dsh.workspace")).expanduser()
        else:
            workspace = paths.data / "dsh/workspace"
        roots = {"state": paths.state, "data": paths.data}
        for member, staged_file in files:
            name = Path(member.name)
            root = roots[name.parts[0]]
            relative = Path(*name.parts[1:])
            if name.parts[:3] == ("data", "dsh", "workspace"):
                root = workspace
                relative = Path(*name.parts[3:])
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            resolved_root = root.resolve()
            if not target.parent.resolve().is_relative_to(resolved_root):
                raise ConfigError(f"restore path escapes its data directory: {target}")
            if target.is_symlink():
                target.unlink()
            if target.exists() and target.is_dir():
                raise ConfigError(
                    f"cannot replace directory with backup file: {target}"
                )
            shutil.copy2(staged_file, target)
    return len(files), total_bytes


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


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        check=check,
        text=True,
        capture_output=not check,
    )
