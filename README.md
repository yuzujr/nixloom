# NixLoom

NixLoom is an opinionated, reproducible local-AI runtime for NixOS. The
included reference profile is tuned for an x86_64 NVIDIA laptop with 8 GiB of
VRAM and 32 GiB of RAM: it serves Qwen3.6 35B-A3B with optional thinking and
vision, swaps to SDXL for image generation, and connects Open WebUI, Hermes
Agent and SillyTavern.

The repository contains the complete runtime definition, pinned dependencies,
configuration contract, asset manifest, service module, tests and control
command. It deliberately does not contain model weights, credentials or
frontend databases.

The supported interface is one command:

```bash
nixloom --help
nixloom start
nixloom status
nixloom logs runtime --follow
nixloom stop
```

Long-running processes are systemd user services under `nixloom.target`.
They are declared by the Home Manager module and deliberately do not start at
login. `nixloom` controls their lifecycle; files under `scripts/` and `tests/`
are packaged implementation details, not additional user entry points.

## Install and update

Normal users do not clone this repository. Add one flake input and import its
Home Manager module; the module supplies the matching package automatically:

```nix
{
  inputs.nixloom.url = "github:yuzujr/nixloom";

  # In the relevant Home Manager module:
  imports = [ inputs.nixloom.homeManagerModules.default ];
  services.nixloom.enable = true;
}
```

After rebuilding Home Manager/NixOS:

```bash
nixloom config check
nixloom models download
nixloom start
```

The flake source, executable, default configuration and asset lock stay
read-only in `/nix/store`. The module creates a separate writable state
directory at `$XDG_DATA_HOME/nixloom` (normally
`~/.local/share/nixloom`) for models, databases, caches and `.env`. Model
downloads remain an explicit, confirmed command; activation and service start
never download anything.

No secret is required for a loopback-only first start. Tavily search is
disabled with a warning until `TAVILY_API_KEY` exists in the state directory's
`.env`. `nixloom env init` previews and creates that private template when it
is needed. Remote exposure and Civitai credentials are opt-in there as well.

Advanced users can change the data location or supply a declarative custom
configuration without cloning NixLoom:

```nix
services.nixloom = {
  stateDir = "/data/nixloom";
  configFile = ./nixloom.yaml;
  # assetLockFile = ./models.lock.yaml;
};
```

Only contributors need a source checkout. For local development, point the
consuming host configuration at that checkout with an absolute `git+file:`
URL, then use `nix develop 'git+file:.'` and
`nix flake check 'git+file:.'`.

Changing NixLoom code or a pinned package does nothing to running services
until that rebuild is explicitly requested. A rebuild installs the new unit
definitions but keeps active NixLoom processes on their old version; run
`nixloom restart` when you want to cut over. Entering the development shell
also has no filesystem or model-loading side effects.

## Intentional side effects

Commands that can consume substantial time, alter persistent state, download
data, swap GPU models, or interrupt services print a plan and ask before doing
anything. They all accept `--dry-run`; automation must pass `--yes`.

| Command | Effect |
| --- | --- |
| `nixloom env init` | Creates a mode-0600 `.env` template after confirmation; never overwrites |
| `nixloom start` | Starts declared services, may migrate frontend state, then warms Qwen; never downloads |
| `nixloom models download [ASSET...]` | Shows files, destination and maximum bytes before downloading |
| `nixloom test smoke` | Sends live requests; the default SD case unloads and later reloads Qwen |
| `nixloom bench` | Runs sustained inference and writes one result under `.benchmarks/` |
| `nixloom bench cpu` | Stops the services, runs the 9955HX affinity matrix, then restores them |
| `nixloom backup [DEST]` | Stops the services, snapshots private state, verifies the archive, then restores them |

Read-only inspection does not ask for confirmation:

```bash
nixloom status
nixloom health
nixloom config check
nixloom models check
nixloom logs webui
```

## Configuration ownership

The packaged `config.yaml` is the default runtime profile. Large asset URLs,
exact sizes and hashes live in the packaged `models.lock.yaml`. Both are
immutable and versioned with the flake; `services.nixloom.configFile` and
`assetLockFile` provide explicit override points. Secrets live in `.env` under
the writable state directory:

```text
TAVILY_API_KEY=...
SILLYTAVERN_AUTH_PASSWORD=...
CIVITAI_API_TOKEN=...
```

Host identity and exposure settings live in that same `.env`; secrets and
machine-local values share one owner, backup policy and service lifecycle.
This keeps personal IP addresses, DNS names and usernames out of the reusable
profile without introducing a second override layer:

```text
NIXLOOM_REMOTE=true
NIXLOOM_WEBUI_ORIGINS='http://127.0.0.1:3000;https://ai.example.net'
SILLYTAVERN_AUTH_USER=alice
```

The committed defaults bind frontends only to loopback. Remote exposure is
opt-in and requires authentication. `NIXLOOM_ROOT` is an internal name for the
writable state directory, not a source checkout.

The contract has no model registry, inheritance layer, service-local default,
or duplicated Nix option for model settings:

- `llm:` owns model placement, context and normal/thinking sampling.
- `images:` owns SD profiles and selects the active checkpoint.
- `deployment.frontends` determines which declared frontend units pass their
  systemd `ExecCondition`.
- `webui:`, `hermes:` and `sillytavern:` own frontend behavior only.
- unknown and missing config keys fail validation.
- config model paths and the asset lock must match exactly.

Mutable state remains under the state directory (`.webui`, `.sillytavern`,
`.hermes`, `.hf`, `models`). Executables, default configuration and
dependencies come from the Nix store, so services never execute mutable
repository scripts.

## One model, three capabilities

There is one public API model ID, currently `qwen`. Text and attached images
use the same loaded process; the multimodal projector remains in CPU RAM.
Thinking is request-level, not another set of weights. Open WebUI exposes a
virtual `qwen-think` Workspace model that sends:

```json
{
  "chat_template_kwargs": {"enable_thinking": true},
  "thinking_budget_tokens": -1
}
```

`llama-server --jinja` applies the chat template embedded in the GGUF. Clients
send structured messages and must not apply a second template.

The Open WebUI system prompt is composed from the modules under
`webui.system_prompt`. Tool instructions are included only when the matching
built-in tool is enabled. SillyTavern is synchronized to the normal profile;
characters, chats and prompt layout remain user-owned. Hermes uses the same
model and adds its own tool prompt.

SD is the only separate model process. Calls through `/upstream/sd` make
llama-swap unload Qwen, load KoboldCpp, and restore Qwen on the next chat.

## Tests and benchmark results

```bash
nixloom test smoke --skip-sd
nixloom test smoke
nixloom test hermes
nixloom bench --suite quality --quality-profile extended
nixloom bench --suite quality --prompt-profile control
nixloom bench --suite quality --prompt-profile candidate \
  --candidate-prompt-file /path/to/prompt.txt
nixloom bench cpu
```

The smoke gate covers the embedded chat template, chat, request-level
thinking, vision and optional SD swapping. The fixed benchmark records TTFT,
prompt and generation speed, VRAM, RAM, response quality, prompt profile and
prompt hash. The CPU matrix compares 8/12/16 threads and single/dual-CCD
affinity. Results are gitignored under `.benchmarks/`.

## Networking and isolation

With `NIXLOOM_REMOTE=true`, Open WebUI and SillyTavern bind remotely and require
authentication. The model API and Hermes remain on loopback. Restrict the
frontend ports in the host firewall; the reference deployment exposes them
only on Tailscale.

Hermes runs inside bubblewrap. The rest of `/home` and `.env` are hidden, the
repository config and Git metadata are read-only, and its executable launchers
come from the immutable Nix store.

## Backup contents

Backups contain Open WebUI accounts/chats, SillyTavern state, Hermes memory
and secrets. Models, caches, logs and all Nix-provided software are excluded.
Archives default to `~/backups/nixloom` with mode 0600. Keep a separate offline
copy of model assets only if upstream deletion is a concern.
