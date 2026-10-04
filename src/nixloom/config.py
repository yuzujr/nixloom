"""Configuration loading, validation, and XDG path handling."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the user configuration violates the runtime contract."""


@dataclass(frozen=True)
class RuntimePaths:
    state: Path
    data: Path
    cache: Path
    config_dir: Path
    config_file: Path
    share: Path | None

    @classmethod
    def from_environment(cls, explicit_config: str | None = None) -> RuntimePaths:
        home = Path.home()
        state = Path(
            os.environ.get(
                "NIXLOOM_STATE_DIR",
                Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
                / "nixloom",
            )
        ).expanduser()
        data = Path(
            os.environ.get(
                "NIXLOOM_DATA_DIR",
                Path(os.environ.get("XDG_DATA_HOME", home / ".local/share"))
                / "nixloom",
            )
        ).expanduser()
        cache = Path(
            os.environ.get(
                "NIXLOOM_CACHE_DIR",
                Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "nixloom",
            )
        ).expanduser()
        config_dir = Path(
            os.environ.get(
                "NIXLOOM_CONFIG_DIR",
                Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "nixloom",
            )
        ).expanduser()
        share_value = os.environ.get("NIXLOOM_SHARE")
        share = Path(share_value) if share_value else None

        configured = explicit_config or os.environ.get("NIXLOOM_CONFIG_FILE")
        if configured:
            config_file = Path(configured).expanduser()
        elif (config_dir / "config.yaml").is_file():
            config_file = config_dir / "config.yaml"
        elif share and (share / "config.yaml").is_file():
            config_file = share / "config.yaml"
        else:
            source_template = Path(__file__).resolve().parents[2] / "config.yaml"
            config_file = source_template
        return cls(
            state=state.resolve(),
            data=data.resolve(),
            cache=cache.resolve(),
            config_dir=config_dir.resolve(),
            config_file=config_file.resolve(),
            share=share.resolve() if share else None,
        )


class Config:
    """Validated YAML configuration with typed dotted-key access."""

    def __init__(self, value: dict[str, Any], path: Path):
        self.value = value
        self.path = path

    @classmethod
    def load(cls, paths: RuntimePaths) -> Config:
        if not paths.config_file.is_file():
            raise ConfigError(f"config file not found: {paths.config_file}")
        try:
            raw = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ConfigError(f"cannot read {paths.config_file}: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigError(f"{paths.config_file} must contain a YAML mapping")
        config = cls(raw, paths.config_file)
        config.validate()
        return config

    def get(self, dotted: str, default: Any = None, *, required: bool = False) -> Any:
        current: Any = self.value
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                if required:
                    raise ConfigError(f"missing required setting: {dotted}")
                return default
            current = current[part]
        if required and (current is None or current == ""):
            raise ConfigError(f"missing required setting: {dotted}")
        return current

    def model_path(self, value: str, paths: RuntimePaths) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else paths.data / path

    def string(self, key: str, default: str | None = None) -> str:
        value = self.get(key, default, required=default is None)
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be a string")
        return value

    def integer(self, key: str, default: int | None = None, *, minimum: int = 0) -> int:
        value = self.get(key, default, required=default is None)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ConfigError(f"{key} must be an integer >= {minimum}")
        return value

    def number(self, key: str, default: float | None = None) -> float:
        value = self.get(key, default, required=default is None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} must be a number")
        return float(value)

    def boolean(self, key: str, default: bool | None = None) -> bool:
        value = self.get(key, default, required=default is None)
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false")
        return value

    def validate(self) -> None:
        ports: list[int] = []
        configured_ports = self.get("ports", required=True)
        if not isinstance(configured_ports, dict):
            raise ConfigError("ports must be a mapping")
        for name, default in {
            "llama": 8080,
            "dsh": 3080,
            "sillytavern": 8000,
            "swap": 8187,
            "comfyui": 8188,
            "comfy_backend": 8189,
        }.items():
            port = self.integer(f"ports.{name}", default, minimum=1)
            if port > 65535:
                raise ConfigError(f"ports.{name} must be <= 65535")
            ports.append(port)
        if len(ports) != len(set(ports)):
            raise ConfigError("configured ports must be distinct")

        if self.boolean("video.enabled", False):
            if not self.boolean("images.enabled"):
                raise ConfigError(
                    "video requires images.enabled for the ComfyUI runtime"
                )
            for key in ("model_file", "text_encoder", "video_vae", "audio_vae"):
                value = self.string("video." + key)
                if Path(value).is_absolute() or ".." in Path(value).parts:
                    raise ConfigError(
                        f"video.{key} must be relative to the ComfyUI model directory"
                    )
            size = self.string("video.size")
            if not re.fullmatch(r"[0-9]+x[0-9]+", size) or any(
                int(x) < 32 or int(x) % 32 for x in size.split("x")
            ):
                raise ConfigError("video.size must be WIDTHxHEIGHT in multiples of 32")
            frames = self.integer("video.frames", minimum=5)
            if (frames - 5) % 17:
                raise ConfigError(
                    "video.frames must be 17k+5, for example 124 (~5 seconds)"
                )
            self.integer("video.steps", minimum=1)

        self.string("llm.id")
        self.string("llm.model_file")
        self.string("llm.mmproj_file")
        context = self.integer("llm.context", minimum=1)
        maximum = self.integer("llm.max_tokens", minimum=1)
        if maximum >= context:
            raise ConfigError("llm.max_tokens must be smaller than llm.context")
        for key in (
            "fit_target",
            "threads",
            "threads_batch",
            "image_tokens",
        ):
            self.integer(f"llm.{key}", minimum=1)
        n_cpu_moe = self.get("llm.n_cpu_moe")
        if (
            n_cpu_moe is not None
            and n_cpu_moe != "auto"
            and (
                not isinstance(n_cpu_moe, int)
                or isinstance(n_cpu_moe, bool)
                or n_cpu_moe < 0
            )
        ):
            raise ConfigError("llm.n_cpu_moe must be 'auto' or a non-negative integer")
        for key in ("mmap", "flash_attention", "mmproj_offload", "reasoning_preserve"):
            self.boolean(f"llm.{key}")
        for key in ("cache_type_k", "cache_type_v"):
            if self.string(f"llm.{key}") not in {
                "f32",
                "f16",
                "bf16",
                "q8_0",
                "q4_0",
                "q4_1",
                "iq4_nl",
                "q5_0",
                "q5_1",
            }:
                raise ConfigError(f"llm.{key} has an unsupported cache type")
        for key in (
            "temperature",
            "top_p",
            "min_p",
            "frequency_penalty",
            "presence_penalty",
            "repeat_penalty",
        ):
            self.number(f"llm.sampling.{key}")
        self.integer("llm.sampling.top_k", minimum=0)

        images = self.get("images", required=True)
        if not isinstance(images, dict):
            raise ConfigError("images must be a mapping")
        images_enabled = self.boolean("images.enabled")
        image_runtime = os.environ.get("NIXLOOM_IMAGE_RUNTIME", "enabled")
        if image_runtime not in {"enabled", "disabled"}:
            raise ConfigError("NIXLOOM_IMAGE_RUNTIME must be enabled or disabled")
        if images_enabled and image_runtime == "disabled":
            raise ConfigError(
                "images.enabled requires services.nixloom.images.enable in Home Manager"
            )
        if images_enabled:
            reserve = self.number("images.reserve_vram", 0.5)
            if reserve < 0:
                raise ConfigError("images.reserve_vram must be non-negative")
            profiles = self.get("images.profiles", required=True)
            if not isinstance(profiles, dict) or not profiles:
                raise ConfigError("images.profiles must be a non-empty mapping")
            for name, profile in profiles.items():
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
                    or not isinstance(profile, dict)
                ):
                    raise ConfigError(
                        "image profile names must use letters, digits, '_' or '-'"
                    )
                for field in (
                    "model_file",
                    "text_encoder",
                    "vae",
                    "architecture",
                    "task",
                    "size",
                ):
                    if not isinstance(profile.get(field), str) or not profile[field]:
                        raise ConfigError(
                            f"images.profiles.{name}.{field} must be a non-empty string"
                        )
                if profile["architecture"] not in {"zimage", "flux2"}:
                    raise ConfigError(f"unsupported image architecture: {name}")
                if profile["task"] not in {"generate", "edit"}:
                    raise ConfigError(f"unsupported image task: {name}")
                if profile["task"] == "edit" and profile["architecture"] != "flux2":
                    raise ConfigError("reference editing requires flux2")
                if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", profile["size"]):
                    raise ConfigError("image profile size must use WIDTHxHEIGHT")
                width, height = map(int, profile["size"].split("x"))
                if min(width, height) < 64 or width % 16 or height % 16:
                    raise ConfigError(
                        "image dimensions must be >=64 and multiples of 16"
                    )
                if (
                    isinstance(profile.get("steps"), bool)
                    or not isinstance(profile.get("steps"), int)
                    or profile["steps"] < 1
                ):
                    raise ConfigError(
                        f"images.profiles.{name}.steps must be a positive integer"
                    )
                for field in ("model_file", "text_encoder", "vae"):
                    path = Path(profile[field])
                    if path.is_absolute() or ".." in path.parts:
                        raise ConfigError(
                            f"{field} must be relative to its ComfyUI model folder"
                        )
            for task in ("generate", "edit"):
                selected = self.string(f"images.{task}")
                if selected not in profiles or profiles[selected]["task"] != task:
                    raise ConfigError(f"images.{task} must select a {task} profile")

        if "sillytavern" in self.value:
            self.string("sillytavern.preset")
        if "dsh" in self.value:
            version = self.string("dsh.version", "0.2.0-rc.2")
            if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
                raise ConfigError("dsh.version must be an exact npm version")
            workspace = self.string("dsh.workspace", "")
            if workspace and not Path(workspace).is_absolute():
                raise ConfigError("dsh.workspace must be an absolute path")

        assets = self.get("assets", {})
        if not isinstance(assets, dict):
            raise ConfigError("assets must be a mapping")
        for name, asset in assets.items():
            if not isinstance(name, str) or not isinstance(asset, dict):
                raise ConfigError("every asset must be a named mapping")
            relative = asset.get("path")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
            ):
                raise ConfigError(f"assets.{name}.path must be a safe relative path")
            url = asset.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise ConfigError(f"assets.{name}.url must be HTTP(S)")
            size = asset.get("size")
            digest = asset.get("sha256")
            if isinstance(size, bool) or not isinstance(size, int) or size < 1:
                raise ConfigError(f"assets.{name}.size must be a positive integer")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ConfigError(
                    f"assets.{name}.sha256 must be 64 lowercase hexadecimal characters"
                )
