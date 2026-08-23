"""Configuration loading, validation, and XDG path handling."""

from __future__ import annotations

import os
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
                Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
                / "nixloom",
            )
        ).expanduser()
        config_dir = Path(
            os.environ.get(
                "NIXLOOM_CONFIG_DIR",
                Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
                / "nixloom",
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

    def image_profile(self) -> tuple[str, dict[str, Any]]:
        name = self.string("images.profile")
        profile = self.get(f"images.profiles.{name}", required=True)
        if not isinstance(profile, dict):
            raise ConfigError(f"images.profiles.{name} must be a mapping")
        return name, profile

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
        for name in ("llama", "openclaw", "sillytavern"):
            if name not in configured_ports and name != "llama":
                continue
            port = self.integer(f"ports.{name}", minimum=1)
            if port > 65535:
                raise ConfigError(f"ports.{name} must be <= 65535")
            ports.append(port)
        if len(ports) != len(set(ports)):
            raise ConfigError("configured ports must be distinct")

        self.string("llm.id")
        self.string("llm.model_file")
        self.string("llm.mmproj_file")
        context = self.integer("llm.context", minimum=1)
        maximum = self.integer("llm.max_tokens", minimum=1)
        thinking_maximum = self.integer("llm.thinking_max_tokens", minimum=1)
        if maximum >= context or thinking_maximum >= context:
            raise ConfigError("LLM output token limits must be smaller than llm.context")
        for key in (
            "n_cpu_moe",
            "fit_target",
            "threads",
            "threads_batch",
            "image_tokens",
        ):
            self.integer(f"llm.{key}", minimum=1)
        for key in ("mmap", "flash_attention", "mmproj_offload", "reasoning_preserve"):
            self.boolean(f"llm.{key}")
        for key in ("cache_type_k", "cache_type_v"):
            if self.string(f"llm.{key}") not in {
                "f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"
            }:
                raise ConfigError(f"llm.{key} has an unsupported cache type")
        for key in ("temperature", "top_p", "min_p", "frequency_penalty", "presence_penalty", "repeat_penalty"):
            self.number(f"llm.sampling.{key}")
        self.integer("llm.sampling.top_k", minimum=0)

        self.boolean("images.enabled")
        self.string("images.weight_type")
        self.boolean("images.flash_attention")
        name, profile = self.image_profile()
        for field in ("model_file", "size", "sampler", "scheduler", "negative_prompt", "prompt_prefix"):
            if not isinstance(profile.get(field), str):
                raise ConfigError(f"images.profiles.{name}.{field} must be a string")
        for field in ("steps",):
            if isinstance(profile.get(field), bool) or not isinstance(profile.get(field), int) or profile[field] < 1:
                raise ConfigError(f"images.profiles.{name}.{field} must be a positive integer")
        if isinstance(profile.get("cfg_scale"), bool) or not isinstance(profile.get("cfg_scale"), (int, float)):
            raise ConfigError(f"images.profiles.{name}.cfg_scale must be a number")
        lora = profile.get("lora", "")
        if lora and not isinstance(lora, str):
            raise ConfigError(f"images.profiles.{name}.lora must be a string")
        if lora and not isinstance(profile.get("lora_mult"), (int, float)):
            raise ConfigError(f"images.profiles.{name}.lora_mult must be a number when lora is set")

        if "sillytavern" in self.value:
            bind = self.string("sillytavern.bind")
            if not bind:
                raise ConfigError("sillytavern.bind must not be empty")
            self.string("sillytavern.auth_user")
            self.string("sillytavern.auth_password", "")
            self.string("sillytavern.preset")
        if "openclaw" in self.value:
            self.string("openclaw.workspace", "")
            self.boolean("openclaw.yuanbao")

        assets = self.get("assets", {})
        if not isinstance(assets, dict):
            raise ConfigError("assets must be a mapping")
        for name, asset in assets.items():
            if not isinstance(name, str) or not isinstance(asset, dict):
                raise ConfigError("every asset must be a named mapping")
            relative = asset.get("path")
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ConfigError(f"assets.{name}.path must be a safe relative path")
            url = asset.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                raise ConfigError(f"assets.{name}.url must be HTTP(S)")
            size = asset.get("size")
            digest = asset.get("sha256")
            if isinstance(size, bool) or not isinstance(size, int) or size < 1:
                raise ConfigError(f"assets.{name}.size must be a positive integer")
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ConfigError(f"assets.{name}.sha256 must be 64 lowercase hexadecimal characters")
