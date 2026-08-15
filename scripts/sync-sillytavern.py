#!/usr/bin/env python3
"""Synchronize the managed local SillyTavern connection and RP preset."""

import argparse
import json
import os
import tempfile
from pathlib import Path


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        json.dump(value, output, ensure_ascii=False, indent=4)
        output.write("\n")
        temporary_path = Path(output.name)
    os.chmod(temporary_path, mode)
    os.replace(temporary_path, path)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def generation_settings(args: argparse.Namespace) -> dict:
    return {
        "temperature": args.temperature,
        "frequency_penalty": args.frequency_penalty,
        "presence_penalty": args.presence_penalty,
        "top_p": args.top_p,
        "top_k": args.top_k,
        "min_p": args.min_p,
        "repetition_penalty": args.repetition_penalty,
        "max_context_unlocked": True,
        "openai_model": args.model,
        "openai_max_context": args.context,
        "openai_max_tokens": args.max_tokens,
        "reverse_proxy": args.base_url,
    }


def sync(args: argparse.Namespace) -> None:
    settings_path = Path(args.settings)
    if not settings_path.exists():
        print("SillyTavern settings are not initialized; profile sync will run next start.")
        return

    settings = load_json(settings_path)
    preset_dir = settings_path.parent / "OpenAI Settings"
    target_path = preset_dir / f"{args.preset}.json"
    # On later starts, preserve user-edited prompt ordering and roleplay
    # options; only the runtime fields below remain config-owned.
    if target_path.exists():
        preset = load_json(target_path)
    else:
        source_name = settings.get("oai_settings", {}).get(
            "preset_settings_openai", ""
        )
        source_path = preset_dir / f"{source_name}.json"
        preset = (
            load_json(source_path)
            if source_name and source_path.exists()
            else dict(settings.get("oai_settings", {}))
        )
    managed_settings = generation_settings(args)
    preset.update(managed_settings)
    write_json_atomic(target_path, preset)

    oai_settings = settings.setdefault("oai_settings", {})
    oai_settings.update(managed_settings)
    oai_settings["preset_settings_openai"] = args.preset

    profiles = (
        settings.get("extension_settings", {})
        .get("connectionManager", {})
        .get("profiles", [])
    )
    matched_profile = False
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if profile.get("api-url") == args.base_url or (
            profile.get("api") == "openai" and profile.get("proxy") == "llama"
        ):
            profile.update(
                {
                    "api": "openai",
                    "api-url": args.base_url,
                    "model": args.model,
                    "preset": args.preset,
                    "name": args.preset,
                }
            )
            matched_profile = True
            break
    if not matched_profile:
        print("warning: no managed SillyTavern local connection profile was found")

    write_json_atomic(settings_path, settings)
    print(
        f"SillyTavern local profile synchronized: {args.model}, "
        f"{args.context} ctx, {args.max_tokens} max output"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--context", type=int, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--temperature", type=float, required=True)
    parser.add_argument("--frequency-penalty", type=float, default=0)
    parser.add_argument("--presence-penalty", type=float, required=True)
    parser.add_argument("--top-p", type=float, required=True)
    parser.add_argument("--top-k", type=int, required=True)
    parser.add_argument("--min-p", type=float, required=True)
    parser.add_argument("--repetition-penalty", type=float, required=True)
    args = parser.parse_args()
    if args.context < 1 or args.max_tokens < 1 or args.max_tokens >= args.context:
        parser.error("context and max-tokens must satisfy 0 < max-tokens < context")
    if args.temperature < 0:
        parser.error("temperature must be non-negative")
    if not 0 <= args.top_p <= 1 or not 0 <= args.min_p <= 1:
        parser.error("top-p and min-p must be between 0 and 1")
    if args.top_k < 0 or args.repetition_penalty <= 0:
        parser.error("top-k must be non-negative and repetition-penalty positive")
    sync(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
