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

The flake source, executable and default configuration template stay read-only
in `/nix/store`. The Home Manager module creates four separate writable
directories under the XDG base directories:

| Directory | Purpose |
| --- | --- |
| `~/.config/nixloom/` | Your private `config.yaml` (mode 0600) |
| `~/.local/share/nixloom/` | Model weights, `.hf` and embeddings |
| `~/.local/state/nixloom/` | Frontend databases, Hermes state, logs, keys |
| `~/.cache/nixloom/` | Re-downloadable caches (llama-swap config, model caches) |

On first activation the module copies the packaged template to
`~/.config/nixloom/config.yaml` and never overwrites it afterwards. Model
downloads remain an explicit, confirmed command; activation and service start
never download anything.

No secret is required for a loopback-only first start. Tavily search is
disabled with a warning until `credentials.tavily_api_key` is set in your
private `config.yaml`. `nixloom config init` previews and creates that private
template when it is needed. Remote exposure and Civitai credentials are opt-in
there as well.

Advanced users can move the writable directories or supply a declarative custom
configuration without cloning NixLoom:

```nix
services.nixloom = {
  stateDir = "/data/nixloom/state";
  dataDir = "/data/nixloom/data";
  cacheDir = "/data/nixloom/cache";
  configFile = ./nixloom.yaml;
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
| `nixloom config init` | Creates a mode-0600 `config.yaml` template after confirmation; never overwrites |
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

The packaged `config.yaml` is the default runtime profile. On first activation
the module copies it to `~/.config/nixloom/config.yaml`; from then on that
user-owned file is the single source of truth.
`services.nixloom.configFile` provides a declarative override point.

Credentials live only in the private `config.yaml` — never in the repository
or an environment file:

```yaml
credentials:
  tavily_api_key: ...
  civitai_api_token: ...
sillytavern:
  auth_password: ...
```

Host identity and exposure settings live in that same private file; secrets
and machine-local values share one owner, backup policy and service lifecycle.
This keeps personal IP addresses, DNS names and usernames out of the reusable
profile without introducing a second override layer:

```yaml
deployment:
  remote: true
webui:
  cors_allow_origins:
    - http://127.0.0.1:3000
    - https://ai.example.net
sillytavern:
  auth_user: alice
```

The committed defaults bind frontends only to loopback. Remote exposure is
opt-in and requires authentication. The writable directories are user data,
not a source checkout.

The contract has no model registry, inheritance layer, service-local default,
or duplicated Nix option for model settings:

- `llm:` owns model placement, context and normal/thinking sampling.
- `images:` owns SD profiles and selects the active checkpoint.
- `deployment.frontends` determines which declared frontend units pass their
  systemd `ExecCondition`.
- `webui:`, `hermes:` and `sillytavern:` own frontend behavior only.
- unknown and missing config keys fail validation.
- model paths may be absolute or relative to the data directory; the optional
  `assets:` manifest pins files for `nixloom models download` and never
  restricts models you add yourself.

Model weights live under the data directory (`models`, `.hf`). Frontend state
lives under the state directory (`.webui`, `.sillytavern`, `.hermes`), and
re-downloadable caches under the cache directory. Executables, default
configuration and dependencies come from the Nix store, so services never
execute mutable repository scripts.

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

With `deployment.remote: true`, Open WebUI and SillyTavern bind remotely and
require authentication. The model API and Hermes remain on loopback. Restrict
the frontend ports in the host firewall; the reference deployment exposes them
only on Tailscale.

Hermes runs inside bubblewrap. The rest of `/home` and the private
`config.yaml` (and its credentials) are hidden, and its executable launchers
and packaged config template come from the immutable Nix store.

## Backup contents

Backups contain your private `config.yaml`, Open WebUI accounts/chats,
SillyTavern state, Hermes memory and the WebUI secret. Models, caches, logs and
all Nix-provided software are excluded.
Archives default to `~/backups/nixloom` with mode 0600. Keep a separate offline
copy of model assets only if upstream deletion is a concern.
