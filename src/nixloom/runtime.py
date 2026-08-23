"""Command construction and process entry points for model runtimes."""

from __future__ import annotations

import os
import shlex
from typing import Any

import yaml

from .config import Config, ConfigError, RuntimePaths


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def llama_command(
    config: Config,
    paths: RuntimePaths,
    *,
    host: str = "127.0.0.1",
    port: int | str | None = None,
) -> list[str]:
    llm = config.get("llm", required=True)
    assert isinstance(llm, dict)
    sampling = config.get("llm.sampling", required=True)
    assert isinstance(sampling, dict)
    model = config.model_path(config.string("llm.model_file"), paths)
    mmproj = config.model_path(config.string("llm.mmproj_file"), paths)
    gpu_layers = llm["gpu_layers"]
    if not isinstance(gpu_layers, (int, str)) or isinstance(gpu_layers, bool):
        raise ConfigError("llm.gpu_layers must be an integer or 'auto'")
    bind_port = port if port is not None else config.integer("ports.llama", minimum=1)
    command = [
        "llama-server",
        "-m",
        str(model),
        "--mmproj",
        str(mmproj),
        "--alias",
        config.string("llm.id"),
        "--host",
        host,
        "--port",
        str(bind_port),
        "-ngl",
        str(gpu_layers),
        "-c",
        str(config.integer("llm.context", minimum=1)),
        "-n",
        str(config.integer("llm.max_tokens", minimum=1)),
        "--parallel",
        "1",
        "--jinja",
        "--no-ui",
        "--fit",
        "on",
        "--fit-target",
        str(config.integer("llm.fit_target", minimum=1)),
        "--n-cpu-moe",
        str(config.integer("llm.n_cpu_moe", minimum=1)),
        "--threads",
        str(config.integer("llm.threads", minimum=1)),
        "--threads-batch",
        str(config.integer("llm.threads_batch", minimum=1)),
        "--flash-attn",
        _on_off(config.boolean("llm.flash_attention")),
        "--cache-type-k",
        config.string("llm.cache_type_k"),
        "--cache-type-v",
        config.string("llm.cache_type_v"),
        "--image-min-tokens",
        str(config.integer("llm.image_tokens", minimum=1)),
        "--image-max-tokens",
        str(config.integer("llm.image_tokens", minimum=1)),
        "--reasoning",
        "auto",
        "--reasoning-budget",
        "-1",
        "--reasoning-format",
        "deepseek",
        "--chat-template-kwargs",
        '{"enable_thinking":false}',
        "--temp",
        str(sampling["temperature"]),
        "--top-k",
        str(sampling["top_k"]),
        "--top-p",
        str(sampling["top_p"]),
        "--min-p",
        str(sampling["min_p"]),
        "--frequency-penalty",
        str(sampling["frequency_penalty"]),
        "--presence-penalty",
        str(sampling["presence_penalty"]),
        "--repeat-penalty",
        str(sampling["repeat_penalty"]),
        "--mmap" if config.boolean("llm.mmap") else "--no-mmap",
        "--mmproj-offload"
        if config.boolean("llm.mmproj_offload")
        else "--no-mmproj-offload",
        "--reasoning-preserve"
        if config.boolean("llm.reasoning_preserve")
        else "--no-reasoning-preserve",
    ]
    return command


def image_command(
    config: Config,
    paths: RuntimePaths,
    *,
    host: str = "127.0.0.1",
    port: int | str = 7860,
) -> list[str]:
    if not config.boolean("images.enabled"):
        raise ConfigError("image generation is disabled")
    name, profile = config.image_profile()
    model = config.model_path(str(profile["model_file"]), paths)
    command = [
        "sd-server",
        "--model",
        str(model),
        "--listen-ip",
        host,
        "--listen-port",
        str(port),
        "--type",
        config.string("images.weight_type"),
    ]
    lora_value = profile.get("lora", "")
    if lora_value:
        lora = config.model_path(str(lora_value), paths)
        lora_multiplier = profile["lora_mult"]
        command.extend(
            [
                "--lora-model-dir",
                str(lora.parent),
                "--lora-apply-mode",
                "at_runtime",
                # Server APIs deliberately skip request-embedded LoRA tags.
                # Resolve the configured LoRA in the default parameters so
                # native, OpenAI, and A1111 requests inherit it uniformly.
                "--prompt",
                f"<lora:{lora.name}:{lora_multiplier}>",
            ]
        )
    if config.boolean("images.flash_attention"):
        command.append("--diffusion-fa")
    if config.boolean("images.offload_to_cpu", False):
        command.append("--offload-to-cpu")
    if not name:
        raise ConfigError("images.profile must not be empty")
    return command


def swap_document(config: Config, paths: RuntimePaths) -> dict[str, Any]:
    models: dict[str, Any] = {
        config.string("llm.id"): {
            "cmd": "nixloom __service llama --port ${PORT}",
            "checkEndpoint": "/health",
        }
    }
    if config.boolean("images.enabled"):
        models["sd"] = {
            "cmd": "nixloom __service image --port ${PORT}",
            "checkEndpoint": "/v1/models",
            "ttl": 300,
            "unlisted": True,
        }
    return {"healthCheckTimeout": 7200, "logLevel": "info", "models": models}


def swap_command(config: Config, paths: RuntimePaths) -> tuple[list[str], str]:
    target = paths.cache / "llama-swap.yaml"
    command = [
        "llama-swap",
        "--config",
        str(target),
        "--listen",
        f"127.0.0.1:{config.integer('ports.llama', minimum=1)}",
        "--watch-config",
    ]
    document = yaml.safe_dump(swap_document(config, paths), sort_keys=False)
    return command, document


def check_assets(command: list[str], config: Config, paths: RuntimePaths) -> None:
    if command[0] == "llama-server":
        required = [
            config.model_path(config.string("llm.model_file"), paths),
            config.model_path(config.string("llm.mmproj_file"), paths),
        ]
    else:
        _, profile = config.image_profile()
        required = [config.model_path(str(profile["model_file"]), paths)]
        if profile.get("lora"):
            required.append(config.model_path(str(profile["lora"]), paths))
    missing = [path for path in required if not path.is_file()]
    if missing:
        rendered = "\n".join(f"  {path}" for path in missing)
        raise ConfigError(
            f"model assets not found:\n{rendered}\nRun `nixloom models download` first."
        )


def execute(command: list[str]) -> None:
    os.execvp(command[0], command)


def render_command(command: list[str]) -> str:
    return shlex.join(command)
