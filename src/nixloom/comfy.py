"""Managed ComfyUI directories and generation/edit workflows."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .config import Config, ConfigError, RuntimePaths


def node(kind: str, **inputs: Any) -> dict[str, Any]:
    return {"class_type": kind, "inputs": inputs}


def workflow(profile: dict[str, Any], *, edit: bool = False) -> dict[str, Any]:
    """API graphs use only core nodes; the edit path preserves a separate reference."""
    width, height = map(int, profile["size"].split("x"))
    klein = profile["architecture"] == "flux2"
    graph = {
        "1": node(
            "UNETLoader", unet_name=profile["model_file"], weight_dtype="default"
        ),
        "2": node(
            "CLIPLoader",
            clip_name=profile["text_encoder"],
            type="flux2" if klein else "lumina2",
            device="default",
        ),
        "3": node("VAELoader", vae_name=profile["vae"]),
        "4": node("CLIPTextEncode", text="{{prompt}}", clip=["2", 0]),
        "5": node("ConditioningZeroOut", conditioning=["4", 0]),
        "6": node(
            "EmptyFlux2LatentImage" if klein else "EmptySD3LatentImage",
            width=width,
            height=height,
            batch_size=1,
        ),
        "9": node(
            "VAEDecodeTiled",
            samples=["8", 0],
            vae=["3", 0],
            tile_size=512,
            overlap=64,
            temporal_size=64,
            temporal_overlap=8,
        ),
        "10": node(
            "SaveImage",
            images=["9", 0],
            filename_prefix="nixloom/" + ("edit" if edit else "generate"),
        ),
    }
    positive, negative = ["4", 0], ["5", 0]
    if edit:
        if not klein:
            raise ConfigError("reference editing requires a flux2 workflow")
        graph.update(
            {
                "11": node("LoadImage", image="{{image}}"),
                "12": node(
                    "ImageScaleToTotalPixels",
                    image=["11", 0],
                    upscale_method="lanczos",
                    megapixels=1.0,
                    resolution_steps=1,
                ),
                "13": node("VAEEncode", pixels=["12", 0], vae=["3", 0]),
                "14": node("ReferenceLatent", conditioning=positive, latent=["13", 0]),
                "15": node("ReferenceLatent", conditioning=negative, latent=["13", 0]),
                "16": node("GetImageSize", image=["12", 0]),
            }
        )
        positive, negative = ["14", 0], ["15", 0]
        graph["6"]["inputs"].update(width=["16", 0], height=["16", 1])
    if klein:
        graph.update(
            {
                "7": node(
                    "CFGGuider",
                    model=["1", 0],
                    positive=positive,
                    negative=negative,
                    cfg=1.0,
                ),
                "17": node("RandomNoise", noise_seed="{{seed}}"),
                "18": node("KSamplerSelect", sampler_name="euler"),
                "19": node(
                    "Flux2Scheduler",
                    steps=profile["steps"],
                    width=["16", 0] if edit else width,
                    height=["16", 1] if edit else height,
                ),
                "8": node(
                    "SamplerCustomAdvanced",
                    noise=["17", 0],
                    guider=["7", 0],
                    sampler=["18", 0],
                    sigmas=["19", 0],
                    latent_image=["6", 0],
                ),
            }
        )
    else:
        graph["7"] = node("ModelSamplingAuraFlow", model=["1", 0], shift=3.0)
        graph["8"] = node(
            "KSampler",
            model=["7", 0],
            positive=positive,
            negative=negative,
            latent_image=["6", 0],
            seed="{{seed}}",
            steps=profile["steps"],
            cfg=1.0,
            sampler_name="res_multistep",
            scheduler="simple",
            denoise=1.0,
        )
    return graph


def workflows(config: Config) -> dict[str, dict[str, Any]]:
    profiles = config.get("images.profiles", required=True)
    return {
        name: workflow(profile, edit=profile["task"] == "edit")
        for name, profile in profiles.items()
    }


def prepare(config: Config, paths: RuntimePaths) -> Path:
    base = paths.data / "comfyui"
    for directory in (
        base / "models",
        base / "input",
        base / "output",
        paths.state / "comfyui",
        paths.cache / "comfyui",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    directory = base / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    for name, graph in (workflows(config) | video_workflows(config)).items():
        (directory / f"{name}.api.json").write_text(json.dumps(graph, indent=2) + "\n")
    extension = base / "custom_nodes/nixloom_scheduler"
    extension.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        Path(__file__).parent / "comfy_nodes/__init__.py", extension / "__init__.py"
    )
    return base


def command(config: Config, paths: RuntimePaths) -> list[str]:
    base = paths.data / "comfyui"
    result = [
        "comfyui",
        "--listen",
        "127.0.0.1",
        "--port",
        str(config.integer("ports.comfy_backend", 8189, minimum=1)),
        "--base-directory",
        str(base),
        "--user-directory",
        str(paths.state / "comfyui"),
        "--temp-directory",
        str(paths.cache / "comfyui"),
        "--database-url",
        f"sqlite:///{paths.state / 'comfyui/comfyui.db'}",
        "--disable-auto-launch",
        "--disable-api-nodes",
        "--cache-none",
        "--reserve-vram",
        str(config.number("images.reserve_vram", 0.5)),
    ]
    acceleration = os.environ.get("NIXLOOM_ACCELERATION", "cpu")
    if acceleration == "cuda":
        result.extend(["--use-pytorch-cross-attention", "--async-offload", "2"])
    elif acceleration == "cpu":
        result.append("--cpu")
    return result


def run(config: Config, paths: RuntimePaths, *, dry_run: bool = False) -> None:
    launch = command(config, paths)
    if dry_run:
        from .runtime import render_command

        print(render_command(launch))
        return
    prepare(config, paths)
    os.execvp(launch[0], launch)


def video_workflows(config: Config) -> dict[str, dict[str, Any]]:
    if not config.boolean("video.enabled", False):
        return {}
    settings = config.get("video", required=True)
    width, height = map(int, settings["size"].split("x"))
    graph = {
        "1": node(
            "UNETLoader", unet_name=settings["model_file"], weight_dtype="default"
        ),
        "2": node(
            "CLIPLoader",
            clip_name=settings["text_encoder"],
            type="minimax",
            device="default",
        ),
        "3": node("VAELoader", vae_name=settings["video_vae"]),
        "4": node("VAELoader", vae_name=settings["audio_vae"]),
        "5": node(
            "MiniMaxH3ImageToVideo",
            clip=["2", 0],
            vae=["3", 0],
            prompt="{{prompt}}",
            width=width,
            height=height,
            length=settings["frames"],
        ),
        "6": node("BasicGuider", model=["1", 0], conditioning=["5", 0]),
        "7": node("RandomNoise", noise_seed="{{seed}}"),
        "8": node("KSamplerSelect", sampler_name="res_multistep"),
        "9": node(
            "BasicScheduler",
            model=["1", 0],
            scheduler="simple",
            steps=settings["steps"],
            denoise=1.0,
        ),
        "10": node(
            "SamplerCustomAdvanced",
            noise=["7", 0],
            guider=["6", 0],
            sampler=["8", 0],
            sigmas=["9", 0],
            latent_image=["5", 1],
        ),
        "11": node(
            "VAEDecodeTiled",
            samples=["10", 0],
            vae=["3", 0],
            tile_size=512,
            overlap=64,
            temporal_size=16,
            temporal_overlap=4,
        ),
        "12": node("VAEDecodeAudio", samples=["10", 0], vae=["4", 0]),
        "13": node("CreateVideo", images=["11", 0], audio=["12", 0], fps=24.0),
        "14": node(
            "SaveVideo",
            video=["13", 0],
            filename_prefix="nixloom/h3",
            format="auto",
            **{"format.codec": "auto"},
        ),
    }
    import copy

    image = copy.deepcopy(graph)
    image["15"] = node("LoadImage", image="{{image}}")
    image["5"]["inputs"]["first_frame"] = ["15", 0]
    return {"h3-text-to-video": graph, "h3-image-to-video": image}


def browser_workflow(graph: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Build browser graphs from the backend's node schema to keep API and UI aligned."""
    nodes, links = [], []
    inputs: dict[tuple[str, str], int] = {}
    depth: dict[str, int] = {}

    def level(key: str) -> int:
        if key not in depth:
            upstream = [
                level(v[0])
                for v in graph[key]["inputs"].values()
                if isinstance(v, list)
            ]
            depth[key] = max(upstream, default=-1) + 1
        return depth[key]

    rows: dict[int, int] = {}
    for key, api in graph.items():
        definition = schema[api["class_type"]]
        column = level(key)
        row = rows.get(column, 0)
        rows[column] = row + 1
        entry = {
            "id": int(key),
            "type": api["class_type"],
            "pos": [column * 380, row * 350],
            "size": [330, 300],
            "flags": {},
            "order": column,
            "mode": 0,
            "inputs": [],
            "outputs": [],
            "properties": {"Node name for S&R": api["class_type"]},
            "widgets_values": [],
        }
        for section in ("required", "optional"):
            for name, spec in definition["input"].get(section, {}).items():
                kind = spec[0]
                options = spec[1] if len(spec) > 1 else {}
                value = api["inputs"].get(name, options.get("default"))
                widget = isinstance(kind, list) or kind in {
                    "INT",
                    "FLOAT",
                    "STRING",
                    "BOOLEAN",
                    "COMBO",
                    "COMFY_DYNAMICCOMBO_V3",
                }
                if not widget or isinstance(value, list):
                    inputs[key, name] = len(entry["inputs"])
                    entry["inputs"].append(
                        {
                            "name": name,
                            "type": "COMBO" if isinstance(kind, list) else kind,
                            "link": None,
                        }
                    )
                    if widget:
                        entry["inputs"][-1]["widget"] = {"name": name}
                if widget:
                    if isinstance(value, list):
                        value = options.get("default", 0)
                    if value == "{{prompt}}":
                        value = (
                            "A realistic photograph of a woman in a green jacket at a coastal cafe, natural afternoon light, detailed skin texture."
                            if "first_frame" not in api["inputs"]
                            else "The person smiles and turns gently toward the camera. Natural motion and ambient sound."
                        )
                    elif value == "{{seed}}":
                        value = 42
                    elif value == "{{image}}":
                        value = "reference.png"
                    if value is None and isinstance(kind, list):
                        value = kind[0] if kind else ""
                    elif value is None and kind == "COMBO":
                        value = options["options"][0]
                    entry["widgets_values"].append(value)
                    if options.get("control_after_generate"):
                        entry["widgets_values"].append("randomize")
                    if options.get("image_upload"):
                        entry["widgets_values"].append("image")
        for index, kind in enumerate(definition["output"]):
            entry["outputs"].append(
                {"name": definition["output_name"][index], "type": kind, "links": []}
            )
        if api["class_type"] == "SaveVideo":
            entry["widgets_values"] = [api["inputs"]["filename_prefix"], "auto", "auto"]
            entry["widgets_values_named"] = {
                "filename_prefix": api["inputs"]["filename_prefix"],
                "format": "auto",
                "format.codec": "auto",
            }
        nodes.append(entry)
    by_id = {n["id"]: n for n in nodes}
    for key, api in graph.items():
        for name, value in api["inputs"].items():
            if not isinstance(value, list):
                continue
            origin, slot = int(value[0]), value[1]
            target, target_slot = int(key), inputs[key, name]
            link = len(links) + 1
            kind = by_id[origin]["outputs"][slot]["type"]
            links.append([link, origin, slot, target, target_slot, kind])
            by_id[origin]["outputs"][slot]["links"].append(link)
            by_id[target]["inputs"][target_slot]["link"] = link
    return {
        "last_node_id": max(by_id),
        "last_link_id": len(links),
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }


def prepare_browser(
    config: Config, paths: RuntimePaths, schema: dict[str, Any]
) -> None:
    directory = paths.state / "comfyui/default/workflows"
    directory.mkdir(parents=True, exist_ok=True)
    for name, graph in (workflows(config) | video_workflows(config)).items():
        destination = directory / f"NixLoom_{name}.json"
        destination.write_text(
            json.dumps(browser_workflow(graph, schema), indent=2) + "\n"
        )
