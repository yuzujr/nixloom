"""Command construction and process entry points for model runtimes."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

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
    ]
    n_cpu_moe = llm.get("n_cpu_moe", "auto")
    if n_cpu_moe not in (None, "auto", 0):
        command.extend(["--n-cpu-moe", str(n_cpu_moe)])
    command.extend(
        [
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
            "--load-mode",
            "mmap" if config.boolean("llm.mmap") else "none",
            "--mmproj-offload"
            if config.boolean("llm.mmproj_offload")
            else "--no-mmproj-offload",
            "--reasoning-preserve"
            if config.boolean("llm.reasoning_preserve")
            else "--no-reasoning-preserve",
            "--sleep-idle-seconds",
            "31536000",
            "--cache-ram",
            "-1",
        ]
    )
    return command


def check_assets(command: list[str], config: Config, paths: RuntimePaths) -> None:
    required = [
        config.model_path(config.string("llm.model_file"), paths),
        config.model_path(config.string("llm.mmproj_file"), paths),
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        rendered = "\n".join(f"  {path}" for path in missing)
        raise ConfigError(
            f"model assets not found:\n{rendered}\nRun `nixloom models download` first."
        )


def sleep_state_file(paths: RuntimePaths) -> Path:
    return paths.cache / "llama-sleep.state"


def ensure_sleep_hook(paths: RuntimePaths) -> Path:
    if paths.share:
        packaged = paths.share / "libllama_sleep.so"
        if packaged.is_file():
            return packaged
    source = Path(__file__).with_name("llama_sleep.c")
    target = paths.cache / "libllama_sleep.so"
    paths.cache.mkdir(parents=True, exist_ok=True)
    if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-O2",
                str(source),
                "-o",
                str(target),
                "-ldl",
            ],
            check=True,
        )
    return target


def llama_environment(paths: RuntimePaths) -> dict[str, str]:
    hook = ensure_sleep_hook(paths)
    state = sleep_state_file(paths)
    state.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["NIXLOOM_SLEEP_STATE_FILE"] = str(state)
    preload = env.get("LD_PRELOAD", "")
    env["LD_PRELOAD"] = f"{hook}:{preload}" if preload else str(hook)
    return env


def execute(command: list[str], paths: RuntimePaths | None = None) -> None:
    if paths is not None and command[:1] == ["llama-server"]:
        os.environ.update(llama_environment(paths))
    os.execvp(command[0], command)


def render_command(command: list[str]) -> str:
    return shlex.join(command)
