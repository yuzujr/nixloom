# NixLoom

NixLoom is a modular local-AI runtime for NixOS. Its Python control plane runs
one multimodal llama.cpp model and starts stable-diffusion.cpp on demand through
llama-swap. DSH and SillyTavern are independent Home Manager modules rather
than built-in assumptions of the core runtime.

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
an explicit list builds llama.cpp and stable-diffusion.cpp only for those GPU
architectures. Keep this value in the host configuration rather than the
project-wide flake so different machines can select different targets.

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
nixloom open
nixloom logs dsh
```

`nixloom open` opens the current authenticated Web URL in your browser, without
copying a token from the journal. DSH remembers browser login for 30 days, across
service restarts. The default port is 3080.
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
`nixloom dsh run plugin --profile web add <package>` runs DSH's plugin manager
with the same environment as the service. Extra plugins remain user-managed.

NixLoom synchronizes its local model route, context/output limits and Qwen
thinking controls, preserving other providers and plugin settings. When images
are enabled it installs `dsh-image-gen` 0.8.5 and connects its OpenAI-compatible
provider to the existing SD endpoint; no second image runtime is installed.
Image editing still depends on the capabilities of the selected SD backend.

The npm launcher exposes Node internals through Node's own loader because the
upstream native getter does not recognize Nix-built Node. Installation also
corrects DSH's `/bin/bash` default for NixOS and redirects Web's first-use
workspace into the configured NixLoom directory. These workarounds belong to NixLoom
and can be removed when upstream supports this environment directly.

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
- `images`: stable-diffusion.cpp precision and image profiles
- `dsh`: pinned npm version and optional workspace
- `sillytavern`: optional bind, authentication and managed preset
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

`start` and `status` show chat readiness and the next action. Use `nixloom open`
for DSH or `nixloom open sillytavern` for roleplay. `status --verbose` adds service
diagnostics. SillyTavern defaults to local-only access without login; setting a
network bind and password enables password authentication for remote access.

`nixloom test` is the single useful live regression suite. It verifies exact
chat/reasoning/vision results, decodes a generated image, and swaps back to the
LLM. Use `--skip-image` to omit image generation.

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
