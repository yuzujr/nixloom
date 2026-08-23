# NixLoom

NixLoom is a modular local-AI runtime for NixOS. Its Python control plane runs
one multimodal llama.cpp model and starts stable-diffusion.cpp on demand through
llama-swap. OpenClaw and SillyTavern are independent Home Manager modules rather
than built-in assumptions of the core runtime.

## Architecture

```text
OpenClaw ─────── OpenAI chat + OpenAI Images ─┐
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
    openclaw.enable = true;
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

Available modules are `core`, `openclaw`, `sillytavern`, and `default`.
Enabling a frontend creates only that frontend's package and systemd unit.

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
- `openclaw`: optional OpenClaw workspace and Yuanbao channel
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

`nixloom test` is the single useful live regression suite. It verifies normal
chat, reasoning, vision, OpenAI-compatible image generation and swapping back
to the LLM. Use `nixloom test --skip-image` for a fast LLM-only run.

`nixloom backup` temporarily stops the stack and archives only user-owned
configuration plus OpenClaw and SillyTavern state. Models and caches are
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
