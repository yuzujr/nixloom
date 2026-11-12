# NixLoom

NixLoom is a unified local AI runtime integrating chat, image generation/editing, and video workflows under a single GPU gateway scheduler.

```text
DSH Web / SillyTavern ── chat ───────┐
                                     ├── NixLoom Gateway (:8080 chat / :8188 ComfyUI)
DSH Tools / ComfyUI UI ── workflows ─┘     ├── llama-server (:8187, LD_PRELOAD sleep hook)
                                           └── ComfyUI (:8189, /nixloom/unload)
```

- **Transparent VRAM Sharing**: Image/video tasks send `llama-server` into native sleep via `libllama_sleep.so`, saving prompt/KV caches to host RAM. Once ComfyUI finishes, `/nixloom/unload` frees CUDA buffers and wakes `llama-server` with its cache restored.
- **Multimodal Chat**: Multimodal LLM served via `llama-server` with CPU vision encoding (`mmproj_offload: false`) to reserve VRAM for LLM layers.
- **Creative Workflows**:
  - **Generation**: Z-Image-Turbo (INT8 convrot) for fast photographic text-to-image.
  - **Editing**: FLUX.2 Klein (9B NVFP4) with Qwen 3 8B text encoder and full-frame VAE decoding for seamless reference-guided edits.
  - **Video (Optional)**: MiniMax H3 text-to-video and first-frame-to-video with audio.
- **Frontends**: Built-in support for DSH Web (`:3080`), SillyTavern, and direct ComfyUI UI (`:8188`). Includes Whaleness mobile gateway (`:3081`) and Tavily search integration.

---

## Quickstart

```bash
# Check configuration and verify model assets
nixloom config check
nixloom models check

# Download missing assets
nixloom models download

# Service lifecycle
nixloom start
nixloom status
nixloom logs runtime --follow
nixloom stop
```

Other useful commands:
- `nixloom backup` / `nixloom backup restore <archive>`: Snapshot and restore configuration, workspaces, ComfyUI databases, and state (excluding large model weights).
- `nixloom logs all`: Unified log stream across all managed systemd services.

---

## Configuration & Assets

Configuration lives at `~/.config/nixloom/config.yaml`. NixLoom uses standard XDG directories:
- **Config**: `~/.config/nixloom`
- **Models**: `~/.local/share/nixloom`
- **State**: `~/.local/state/nixloom` (DSH workspace, ComfyUI user db)
- **Cache**: `~/.cache/nixloom`

### Licence-Gated Models

Gated models (such as official `FLUX.2-klein-9b-nvfp4`) require licence acceptance on Hugging Face. NixLoom supports two download modes:
1. **Automatic download via token**: Set `credentials.huggingface_token` in `config.yaml` to a read token from an account that accepted the licence. Authorization headers are automatically dropped on CDN redirects.
2. **Manual placement**: Without a token, `nixloom models download` prints the exact repository URL, file name, and destination path. Download via browser and verify with `nixloom models check`.

---

## Home Manager Module

Declare NixLoom in your NixOS / Home Manager configuration:

```nix
{
  imports = [ inputs.nixloom.homeManagerModules.default ];

  services.nixloom = {
    enable = true;
    acceleration = "cuda";          # "cpu", "cuda", "vulkan", or "rocm"
    cudaCapabilities = [ "12.0" ];  # Host-specific build targets (optional)
    images.enable = true;           # Enables ComfyUI image pipeline
    sillytavern.enable = false;     # Optional frontend
    dsh = {
      enable = true;                # Default frontend on port 3080
      tailnet.enable = false;       # Restrict access to Tailnet owner devices
    };
  };
}
```

- Hardware acceleration and enabled frontends are controlled via Nix options, not mutable YAML.
- NixLoom dependencies are pinned independently of the host NixOS channel to prevent unintended CUDA rebuilds.

---

## License

MIT
