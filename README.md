# NixLoom

NixLoom integrates local chat, image generation/editing, and video workflows.
`llama-server` serves the multimodal text model with native sleep hooks; ComfyUI
serves creative workflows. A single gateway schedules GPU access for every frontend.

```text
DSH / SillyTavern ── chat ── NixLoom gateway :8080 ── llama-server :8187 (LD_PRELOAD sleep hook)
ComfyUI browser / DSH image and H3 video tools / SillyTavern
                 └─ workflows ── gateway :8188 ── ComfyUI :8189 (/nixloom/unload)
```

- Image and video requests put `llama-server` into native sleep (via `SIGUSR1`
  and `libllama_sleep.so`), saving the active KV/recurrent prompt cache to host
  RAM before ComfyUI executes the workflow. Chat waits for the actual image/video
  queue to finish, calls `/nixloom/unload` to thoroughly release ComfyUI models,
  cast/prefetch buffers and CUDA caches, then wakes `llama-server` and restores
  its prompt cache. HTTP acceptance does not count as task completion.
- Z-Image-Turbo (INT8 convrot) generates photographs; FLUX.2 Klein (4B NVFP4)
  edits a reference image. Both use native quantized kernels on supported
  Blackwell cards, with an FP8 mixed-precision Qwen text encoder and tiled VAE
  decoding.
- When enabled, MiniMax H3 provides text-to-video and first-frame-to-video
  workflows, including audio, in both DSH and the ComfyUI browser.
- Models, inputs, outputs and extensions live under the data directory;
  browser settings/workflows and the database live under state; temporary files
  live under cache. No mutable files are installed into the Nix store.
- The public ComfyUI URL is `http://localhost:8188/`. With Tailnet access enabled,
  only the owner's Tailscale devices may access it; allow its configured port
  on `tailscale0`. The private backend port must not be exposed or used directly.

## Architecture

```text
DSH Web ──────── OpenAI chat (:8080) + ComfyUI workflows (:8188) ─┐
                                                                  │
SillyTavern ─── OpenAI chat (:8080) + ComfyUI workflows (:8188) ──┼── NixLoom gateway
                                                                  │     ├── llama-server (:8187)
ComfyUI UI ──── Browser workflows & WebSocket (:8188) ────────────┘     └── ComfyUI (:8189)
```

- Text, per-request reasoning and multimodal vision (`mmproj`) share one
  `llama-server` process. Vision encoding runs on CPU (`mmproj_offload: false`)
  with a bounded token budget (`image_tokens: 512`) so VRAM remains dedicated to
  LLM layers and the KV cache.
- Image and video generation use ComfyUI through the NixLoom scheduler gateway.
- Model switching is transparent: a ComfyUI prompt request sleeps `llama-server`
  while preserving the main conversation's prompt cache in RAM across short
  tasks, and the next chat request unloads ComfyUI and wakes `llama-server` with
  re-fitted device parameters.
- The runtime is Python; shell launchers are not part of the package.
- No network-overlay policy is embedded in the project. Bind addresses belong
  to application configuration.

## Platforms and acceleration

The flake exports packages for `x86_64-linux` and `aarch64-linux`. Hardware is
selected in Home Manager rather than encoded into `config.yaml`:

```nix
services.nixloom = {
  enable = true;
  acceleration = "cuda"; # cpu, cuda, vulkan, or rocm
  cudaCapabilities = [ "12.0" ]; # optional, host-specific build target
  images.enable = true; # omit the image backend and its closure when false
};
```

`llamaPackage` and `imagePackage` may also be overridden independently. Image
generation is omitted from the runtime closure unless `images.enable = true`.
The default is CPU, so importing the module does not imply an NVIDIA system.
Backend packages come from NixLoom's own lock rather than the host package set;
updating an unrelated NixOS flake input therefore does not trigger a CUDA
rebuild. Updating NixLoom's lock or overriding a package remains an explicit
runtime upgrade. `cudaCapabilities = [ ];` keeps the portable nixpkgs defaults;
an explicit list builds llama.cpp for those GPU architectures. Set this in the
consuming host configuration, not in NixLoom. ComfyUI's CUDA runtime uses the
community project's independent dependency pin and portable prebuilt wheels,
including NVIDIA's NVSHMEM binary. Host architecture settings do not rebuild
this environment. Binary availability takes priority over sharing CUDA libraries
with llama.cpp.

## Modules

Import the complete option set and enable only the frontends you want:

```nix
{
  imports = [ inputs.nixloom.homeManagerModules.default ];

  services.nixloom = {
    enable = true;
    acceleration = "cuda";
    images.enable = true;
    sillytavern.enable = true;
  };
}
```

The modules are independently importable:

```nix
imports = [ inputs.nixloom.homeManagerModules.sillytavern ];
services.nixloom = {
  enable = true;
  sillytavern.enable = true;
};
```

Available modules are `core`, `dsh`, `sillytavern`, and `default`.
Enabling a frontend creates only that frontend's package and systemd unit.

### DSH Web through npm

DSH is the default frontend when NixLoom is enabled. NixLoom installs the
pinned npm release and plugins when the DSH service starts, configures the local
providers, and supplies Node, pnpm and the systemd user unit. No separate
installation command is needed. Set `services.nixloom.dsh.enable = false` for
a runtime without DSH.

```bash
nixloom start
nixloom logs dsh
```

DSH is served at `http://127.0.0.1:3080/` without a login token. The NixLoom
launcher enables local access for its managed service; the upstream
Host/Origin checks still protect API requests. Direct DSH invocations retain
upstream authentication. SillyTavern is also loopback-only and needs no login.

Startup downloads packages only when the pinned version or a managed plugin
is missing; subsequent starts reuse them. DSH version is pinned and validated
by NixLoom patches. Installation needs network access
when a new package is first added; a failed download fails startup instead of
leaving a partial npm release.

- program and npm lock: `~/.cache/nixloom/dsh/releases/<version>`
- npm/pnpm/native caches: `~/.cache/nixloom/dsh`
- configuration, plugins, sessions and attachments: `~/.local/state/nixloom/.dsh`
- default workspace: `~/.local/share/nixloom/dsh/workspace`

Custom NixLoom XDG paths apply to all four locations. `dsh.workspace` may select
another absolute directory; a working directory is not an access sandbox.
`nixloom dsh plugin --profile web add <package>` runs DSH's plugin manager
with the same environment as the service. Extra plugins remain user-managed.

NixLoom synchronizes its local model route (`input: ["text", "image"]`),
context/output limits and Qwen thinking controls, preserving other providers and
plugin settings. DSH is internally pinned to `0.2.2-rc.1`. When images are
enabled it installs `dsh-image-gen` `0.10.3`, configures its native ComfyUI
provider with named `z-image` and `klein-edit` workflows, and injects local
diffusion prompt and workflow-routing instructions into the DSH system prompt.
Generated and edited images are saved to the workspace and returned as
multimodal image attachments so the Agent can inspect outputs and chain follow-up
edits or video generation. When video is enabled, the managed workspace plugin
also registers `generate_video` for MiniMax H3 text-to-video and
first-frame-to-video with standardized English cinematic prompt rules. It submits
through the NixLoom ComfyUI gateway, waits for the shared GPU scheduler to
finish, and returns the video as a downloadable DSH file attachment. The same H3
graphs remain available in the ComfyUI browser.

NixLoom also installs `dsh-plugin-mobile-gateway` `0.12.0` for the Whaleness
mobile client. The gateway starts in persistent mode with device authentication
required (`adminLoopbackOnly: true`) and enables the dedicated LAN/Tailnet
listener on `0.0.0.0:3081`. Pair Whaleness over LAN or Tailscale using port
`3081` (or `ws://<nixloom-host>:3080/ws/mobile` when Tailnet access is enabled
on port 3080). The paired device credential is retained across restarts; revoke
the device in DSH's Mobile Devices panel to remove its access.

The launcher exposes Node internals through Node's own loader because the upstream
native getter does not recognize Nix-built Node. Bash uses the native `shellPath`
option; the workspace plugin uses `workspaceRegistry.initializeDefault`, preserving
existing registrations. Neither needs a source patch.

The remaining authentication and wildcard-binding changes are explicit unified
diffs (`managed-access.patch` and `wildcard-host.patch`) under
`src/nixloom/dsh-patches`, with SHA-256 checks before and after application.
`nixloom-dsh.service` runs a preparation step (`--prepare-only`) before launch
to install missing packages, apply/verify patches and synchronize the profile,
then verifies the profile fingerprint before executing DSH.

### Direct Tailnet HTTP access

Enable `services.nixloom.dsh.tailnet.enable` and allow the configured DSH port
(default 3080, plus 3081 if using the dedicated mobile gateway listener) only on
`networking.firewall.interfaces.tailscale0.allowedTCPPorts`. DSH then uses the
upstream Web server's explicit `0.0.0.0` configuration, so the
interface-specific firewall rule is required. LAN interfaces must not allow that
port unless intended. Tailscale Serve and Funnel are not involved.

Open `http://<machine>.<tailnet>.ts.net:3080/` or the machine's Tailscale IPv4
address. The managed access check compares the actual TCP peer address with
Tailscale's device inventory for the server owner's account; it does not trust
forwarded headers or a loopback Host supplied by a remote caller. The upstream
Host/Origin checks still apply to API and WebSocket requests. Device grants are
refreshed at service start, so restart DSH after adding an owner device.

The mobile browser works over this HTTP connection. Installing a PWA and enabling
Web Push normally requires HTTPS; a native Android client is a separate option.

## Configuration

The mutable YAML configuration normally lives at
`~/.config/nixloom/config.yaml`. Model files, runtime state and caches use
separate XDG directories:

- config: `~/.config/nixloom`
- models: `~/.local/share/nixloom`
- state: `~/.local/state/nixloom`
- cache: `~/.cache/nixloom`

The important YAML sections are:

- `llm`: model, vision projector (`mmproj_file`, `mmproj_offload`, `image_tokens`), context, `fit_target`, reasoning and sampling settings
- `images`: ComfyUI image profiles, generation/edit defaults, and VRAM reserve
- `video`: optional MiniMax H3 weights, resolution, frame count and steps
- `dsh`: optional workspace path (DSH version is pinned internally to match validated patches)
- `sillytavern`: managed preset
- `credentials`: Tavily search API key, optional Civitai download token and optional HuggingFace read token
- `assets`: pinned downloadable files with exact sizes and SHA-256 hashes; `gated: true` marks licence-gated files

Frontend selection and GPU backend do not live in YAML; those are Nix module
decisions.

Licence-gated assets (e.g. the official FLUX.2 Klein 9B NVFP4 weights) cannot be
downloaded anonymously. `nixloom models pull` handles them in one of two ways:

- set `credentials.huggingface_token` to a read token from a HuggingFace account
  that has accepted the model licence, and NixLoom downloads and verifies the
  file itself (the token is never sent to CDN redirect hosts); or
- without a token it prints the repository page, the file name and the exact
  target path. Download the file in a browser, move it to that path and run
  `nixloom models pull` again to verify size and SHA-256.

## Commands

```bash
nixloom config check
nixloom models check
nixloom models download
nixloom start
nixloom status
nixloom logs runtime --follow
nixloom stop
```

`start`, `restart`, and `stop` are idempotent. Startup reports service readiness
and model-loading time instead of staying silent. `status` combines systemd,
HTTP health, and the currently loaded model in one table; it exits nonzero when
the stack is stopped or degraded. `logs all` merges the journals of every
installed NixLoom service.

Without asset names, `models check` and `models download` select the configured
LLM, the selected image generation/edit profiles, and video weights only when
`video.enabled` is true. Pass asset names explicitly to fetch optional assets
before enabling their feature.

`start` reports each service and model transition once, followed by the URLs.
`status` shows one compact service table and model state; `status --verbose`
adds systemd and health details. Running `nixloom`, `nixloom config` or `nixloom models` without an action shows the
corresponding help.

Web search uses the packaged Tavily provider with `credentials.tavily_api_key`.
The key is passed only in the server process environment, never written into
the plugin profile or Nix store. DSH's original `web_search` tool and HTTP fetch
provider stay in use.

`nixloom backup` temporarily stops the stack and archives the NixLoom
configuration, DSH profiles and workspace, all SillyTavern XDG data, ComfyUI
database and user workflows, plus ComfyUI input and output files. It excludes
downloaded models, caches and DSH's reinstallable `node_modules` directories.
Restore an archive with `nixloom backup restore ARCHIVE`; add `--dry-run` to
preview it. Restore merges files into the current data directories, replacing
same-name files while keeping files absent from the archive. It first creates
a safety backup in `~/backups/nixloom`. The active `config.yaml` is kept because
NixOS manages it; the archived copy is available for manual recovery.

## Development

```bash
nix develop
```

## License

MIT


## ComfyUI workflows

Open the saved `NixLoom_z-image` or `NixLoom_klein-edit` workflow from the browser
workflow sidebar. Edit the prompt and resolution, upload a reference for Klein,
and queue the workflow. Managed workflows are rebuilt from the same API graphs
used by DSH, using the running backend's node schemas. Save custom variations
under another name to keep them across restarts.

`images.reserve_vram` reserves additional headroom for desktop growth and transient CUDA use;
it does not cap compute utilization. ComfyUI dynamically offloads model weights
as necessary. CUDA uses PyTorch attention, asynchronous offloading, and no output
cache so prior graphs do not accumulate VRAM. The default is 0.5 GiB beyond current desktop allocations; measure
actual desktop use before reducing it. The 8 GB deployment uses Klein 4B NVFP4;
a larger model can be configured by changing its weights and matching encoder.

H3 uses `h3-text-to-video` and `h3-image-to-video` workflows. NixLoom generates
these from its managed graphs and the running ComfyUI node schema; they appear in
DSH and the browser workflow list when `video.enabled` is true. The four
required H3 weights have pinned URLs, sizes, and SHA-256 hashes in the shipped
asset catalog. `nixloom models download` includes them when `video.enabled` is
true. To fetch them separately while video is disabled, request them explicitly:

```bash
nixloom models download \
  minimax_h3_nvfp4 \
  qwen3vl_32b_minimax_h3_nvfp4_awq \
  minimax_h3_video_vae_fp16 \
  minimax_h3_audio_vae_fp32
```

Its default 124 frames are approximately five seconds at 24 fps. Large video
models also consume host RAM; low VRAM offloading can increase latency
substantially.
