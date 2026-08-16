# NixLoom

A reproducible local-AI runtime for NixOS. One flake input wires up a
llama.cpp LLM (with optional thinking and vision), SDXL image generation, and
three frontends — Open WebUI, Hermes Agent and SillyTavern — as systemd user
services under `nixloom.target`.

The reference profile targets an x86_64 NVIDIA laptop with 8 GiB of VRAM and
32 GiB of RAM, serving Qwen3.6 35B-A3B.

## Features

- One loaded LLM; request-level thinking and vision reuse the same process
- SDXL on demand — llama-swap unloads the LLM, loads KoboldCpp, then restores it
- Open WebUI, Hermes Agent and SillyTavern frontends
- A single `nixloom` command controls the whole stack
- Config, models, state and caches live in separate XDG directories
- No secret is required for a loopback-only first start

## Requirements

- NixOS with Home Manager
- An NVIDIA GPU (the reference profile uses 8 GiB of VRAM)

## Installation

Add the flake input and import the Home Manager module:

```nix
{
  inputs.nixloom.url = "github:yuzujr/nixloom";
  # In the relevant Home Manager module:
  imports = [ inputs.nixloom.homeManagerModules.default ];
  services.nixloom.enable = true;
}
```

The module installs the `nixloom` command and declares the systemd user
services. Nothing starts until you run `nixloom start`.

## Quick start

```bash
nixloom config check     # validate your config and generated launch commands
nixloom models download  # download the pinned models
nixloom start            # start services and warm the LLM
```

## Usage

| Command | What it does |
| --- | --- |
| `nixloom start` / `stop` / `restart` | Start, stop or restart the services (and warm the LLM on start) |
| `nixloom status` / `health` | Show service/endpoint state; strict health check |
| `nixloom logs [SERVICE]` | Read the systemd journal |
| `nixloom config check` / `init` | Validate config, or create the private template |
| `nixloom models check` / `download` | Verify or download the pinned models |
| `nixloom test [smoke\|hermes]` | Live regression tests |
| `nixloom bench` | Fixed performance/quality benchmark |
| `nixloom backup [DEST]` | Snapshot config and state into a private archive |

State-changing commands print a plan and ask before doing anything; pass
`--yes` to run non-interactively, `--dry-run` to preview.

## Configuration

The first activation copies the packaged `config.yaml` template to
`~/.config/nixloom/config.yaml`; from then on that user-owned file is the
single source of truth, including API credentials. `nixloom config init`
creates it if you do not use the module.

Key sections:

- `llm:` — model placement, context, sampling
- `images:` — SDXL profiles and the active checkpoint
- `webui:` / `hermes:` / `sillytavern:` — frontend behavior
- `assets:` — optional download catalog for `nixloom models download`

Model paths may be absolute or relative to the model data directory.
`services.nixloom.{stateDir,dataDir,cacheDir,configFile}` move the writable
directories to custom locations.

## How it works

One model ID serves text, thinking and vision through the same loaded process;
the multimodal projector stays in CPU RAM. Open WebUI exposes a virtual
`qwen-think` model that enables request-level thinking.

SD is the only separate model process. Image generation goes through
llama-swap's `/upstream/sd`, which swaps the LLM out and KoboldCpp in for the
duration of the request.

## Tests

```bash
nixloom test smoke          # chat, thinking, vision, optional SD
nixloom test hermes         # Hermes tool-call regression
nixloom bench               # TTFT / token speed / quality
nixloom bench cpu           # CPU thread/CCD affinity matrix
```

## Backup

`nixloom backup` stops the services, snapshots your config, frontend
databases, Hermes state and the WebUI secret into a verified private archive
(default `~/backups/nixloom`). Models and caches are excluded and can be
re-downloaded.

## License

MIT
