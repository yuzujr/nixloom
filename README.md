# NixLoom

NixLoom integrates local chat, image generation/editing, and video workflows.
llama.cpp serves the text model through llama-swap; ComfyUI serves creative
workflows. A single gateway schedules GPU access for every frontend.

```text
DSH / SillyTavern ── chat ── NixLoom gateway :8080 ── llama-swap :8187 ── llama.cpp
ComfyUI browser / DSH image tools / SillyTavern
                 └─ workflows ── gateway :8188 ── ComfyUI :8189
```

- Image requests unload the text model before ComfyUI executes the workflow.
  Chat waits for the actual image/video queue to finish, unloads ComfyUI models,
  then loads the text model. HTTP acceptance does not count as task completion.
- Z-Image-Turbo generates photographs; FLUX.2 Klein edits a reference image.
  Both use native NVFP4 kernels on supported Blackwell cards, with quantized
  Qwen text encoders and tiled VAE decoding.
- Optional MiniMax H3 provides text-to-video and first-frame-to-video workflows,
  including audio, in the ComfyUI browser. DSH's image plugin remains for images.
- Models, inputs, outputs and extensions live under the data directory;
  browser settings/workflows and the database live under state; temporary files
  live under cache. No mutable files are installed into the Nix store.
- The public ComfyUI URL is `http://localhost:8188/`. With Tailnet access enabled,
  only the owner's Tailscale devices may access it; allow its configured port
  on `tailscale0`. The private backend port must not be exposed or used directly.

## Architecture

```text
DSH Web ──────── OpenAI chat + OpenAI Images ─┐
                                              │
SillyTavern ─── OpenAI chat + sdcpp source ───┼── llama-swap
                                              │     ├── llama-server
other clients ─ OpenAI / sdcpp / A1111 APIs ──┘     └── sd-server (on demand)
```

- Text, per-request reasoning and vision share one llama.cpp process.
- Image generation uses stable-diffusion.cpp. Its native/OpenAI interfaces are
  the primary integrations; its A1111 API remains available for compatibility.
- Model switching is transparent: an image request unloads the LLM, starts the
  image runtime, and the next chat request restores the LLM.
- The runtime is Python. Shell launchers and duplicated test harnesses are not
  part of the package.
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

DSH is the default frontend when NixLoom is enabled. Home Manager installs the
pinned npm release and image plugin during activation, configures the local
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

Activation downloads packages only when the configured version or image plugin
is missing; subsequent activations reuse them. Change `dsh.version` in YAML
and activate Home Manager again to upgrade. Installation needs network access
on the first activation; a failed download fails activation instead of leaving
a partial npm release. Service startup does not download packages.

- program and npm lock: `~/.cache/nixloom/dsh/releases/<version>`
- npm/pnpm/native caches: `~/.cache/nixloom/dsh`
- configuration, plugins, sessions and attachments: `~/.local/state/nixloom/.dsh`
- default workspace: `~/.local/share/nixloom/dsh/workspace`

Custom NixLoom XDG paths apply to all four locations. `dsh.workspace` may select
another absolute directory; a working directory is not an access sandbox.
`nixloom dsh plugin --profile web add <package>` runs DSH's plugin manager
with the same environment as the service. Extra plugins remain user-managed.

NixLoom synchronizes its local model route, context/output limits and Qwen
thinking controls, preserving other providers and plugin settings. When images
are enabled it installs `dsh-image-gen` 0.8.5 and configures its native ComfyUI
provider with named `z-image` and `klein-edit` workflows. Select the edit workflow
and supply an image for reference editing; the same graphs are saved as browser
workflows in ComfyUI. The plugin currently supports one reference image. Browser
workflows can be expanded to accept multiple references.
Image editing still depends on the capabilities of the selected SD backend.

The launcher exposes Node internals through Node's own loader because the upstream
native getter does not recognize Nix-built Node. Bash uses the native `shellPath`
option; the workspace plugin uses `workspaceRegistry.initializeDefault`, preserving
existing registrations. Neither needs a source patch.

The remaining authentication change is an explicit unified diff under
`src/nixloom/dsh-patches`, with SHA-256 checks before and after application. Home
Manager applies it during installation and removes the previous inline patches.
Startup checks the installed files and profile fingerprint, then executes DSH;
it never patches packages, prepares profiles or installs dependencies. Changes to
managed frontend settings require Home Manager activation again.

### Direct Tailnet HTTP access

Enable `services.nixloom.dsh.tailnet.enable` and allow the configured DSH port
(default 3080) only on `networking.firewall.interfaces.tailscale0.allowedTCPPorts`.
DSH then uses the upstream Web server's explicit `0.0.0.0` configuration, so the
interface-specific firewall rule is required. LAN interfaces must not allow that
port. Tailscale Serve and Funnel are not involved.

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

- `llm`: model, vision projector, context, reasoning and sampling settings
- `images`: ComfyUI image profiles, generation/edit defaults, and VRAM reserve
- `video`: optional MiniMax H3 weights, resolution, frame count and steps
- `dsh`: pinned npm version and optional workspace
- `sillytavern`: managed preset
- `credentials`: Tavily search API key and optional Civitai download token
- `assets`: pinned downloadable files with exact sizes and SHA-256 hashes

Frontend selection and GPU backend do not live in YAML; those are Nix module
decisions.

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

`start` reports each service and model transition once, followed by the URLs.
`status` shows one compact service table and model state; `status --verbose`
adds systemd and health details. Running `nixloom`, `nixloom config` or `nixloom models` without an action shows the
corresponding help.

Web search uses the packaged Tavily provider with `credentials.tavily_api_key`.
The key is passed only in the server process environment, never written into
the plugin profile or Nix store. DSH's original `web_search` tool and HTTP fetch
provider stay in use.

`nixloom test` is the single useful live regression suite. It verifies exact
chat/reasoning/vision results, generates an image, edits that image with Klein,
and swaps back to the LLM. Use `--skip-image` to omit image tests.

`nixloom backup` temporarily stops the stack and archives only user-owned
configuration plus DSH and SillyTavern state. Models, caches and DSH's
reinstallable `node_modules` directories are
excluded.

## Development

```bash
nix develop
python -m unittest discover -s tests -v
ruff check src tests
nix flake check
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

H3 uses `h3-text-to-video` and `h3-image-to-video` browser workflows. NixLoom
generates these from its managed graphs and the running ComfyUI node schema;
they appear in the browser workflow list when `video.enabled` is true. The four
required H3 weights have pinned URLs, sizes, and SHA-256 hashes in the shipped
asset catalog. Download them explicitly, then enable `video.enabled` in the
YAML config:

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
