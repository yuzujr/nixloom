{
  lib,
  pkgs,
  source,
}:
let
  nixloom = pkgs.python3Packages.buildPythonApplication {
    pname = "nixloom";
    version = "0.2.0";
    src = source;
    pyproject = true;
    build-system = [ pkgs.python3Packages.setuptools ];
    dependencies = [ pkgs.python3Packages.pyyaml ];
    nativeBuildInputs = [ pkgs.makeWrapper ];
    nativeCheckInputs = [ pkgs.python3Packages.pyyaml ];
    checkPhase = ''
      runHook preCheck
      PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}" python -m unittest discover -s tests -v
      runHook postCheck
    '';
    pythonImportsCheck = [ "nixloom" ];
    postInstall = ''
      mkdir -p "$out/share/nixloom"
      cp config.yaml "$out/share/nixloom/config.yaml"
    '';
    postFixup = ''
      wrapProgram "$out/bin/nixloom" \
        --set NIXLOOM_SHARE "$out/share/nixloom"
    '';
  };

  yuanbao = pkgs.buildNpmPackage {
    pname = "openclaw-plugin-yuanbao";
    version = "2.18.2";
    src = ./openclaw-yuanbao;
    npmDepsHash = "sha256-Z8x0z5Uq6EvVTFNLipkbLfDkOu3cbS7t507tFW8W0Bo=";
    dontNpmBuild = true;
    installPhase = ''
      runHook preInstall
      mkdir -p "$out"
      cp -r node_modules/openclaw-plugin-yuanbao/. "$out/"
      cp -r node_modules "$out/node_modules"
      rm -rf "$out/node_modules/openclaw-plugin-yuanbao"
      runHook postInstall
    '';
  };

  tavily = pkgs.buildNpmPackage {
    pname = "openclaw-plugin-tavily";
    version = "2026.6.33";
    src = ./openclaw-tavily;
    npmDepsHash = "sha256-0pHKaa0OtFyAkOn6FaBltVfdk5gSdeIx774lo4QLI2U=";
    dontNpmBuild = true;
    installPhase = ''
      runHook preInstall
      mkdir -p "$out"
      cp -r node_modules/@openclaw/tavily-plugin/. "$out/"
      runHook postInstall
    '';
  };

  # Keep the core and bundled Tavily plugin on the nixpkgs release line.  The
  # locked nixpkgs already carries the fixed-output hash for OpenClaw 2026.6.33;
  # an earlier override pinning a different pnpmDepsHash is gone because it went
  # stale when nixpkgs-unstable moved and only surfaced on the first rebuild.
  #
  # NixLoom owns the complete Control UI source tree at
  # nix/openclaw-ui.  It is built against the exact OpenClaw source and pnpm
  # runtime dependency set used by the gateway, so a protocol/UI update is an
  # explicit, reproducible change rather than a runtime DOM injection.
  #
  # The local stable-diffusion.cpp profile supports 512x512 but not OpenClaw's
  # implicit 1024x1024 default.  Teach the OpenAI-compatible provider that
  # 512x512 is supported: otherwise its closest-size normalization silently
  # rewrites an explicit 512x512 request back to 1024x1024.  Also backport a
  # narrowly scoped local-media switch: upstream detaches every session-bound
  # media task, whereas NixLoom
  # needs a completed local file in the same turn to deliver it as MEDIA.  The
  # switch applies only when the NixLoom service opts in through its environment
  # and leaves OpenClaw's default asynchronous behavior unchanged elsewhere.
  # OpenClaw's Control UI WebSocket accepts verified Tailscale identity, but
  # its companion bootstrap and assistant-media HTTP routes used a different
  # authorizer that explicitly disabled it.  Use the same Control UI auth
  # surface for those read-only routes so the page and its attachments cannot
  # disagree about whether an already-connected operator is authorized.
  # Keep these release-specific substitutions fail-closed: an upstream bundle
  # layout change must fail the build rather than silently change semantics.
  openclaw = pkgs.runCommand "nixloom-openclaw-${pkgs.openclaw.version}" {
    nativeBuildInputs = [ pkgs.makeWrapper ];
  } ''
    cp -a ${pkgs.openclaw}/. "$out"
    chmod -R u+w "$out"
    substituteInPlace "$out/lib/openclaw/dist/image-generation-provider-B110FEwo.js" \
      --replace-fail 'const DEFAULT_SIZE = "1024x1024";' 'const DEFAULT_SIZE = "512x512";'
    imageProvider="$out/lib/openclaw/dist/image-generation-provider-B110FEwo.js"
    sed -i '/const OPENAI_SUPPORTED_SIZES = \[/a\	"512x512",' "$imageProvider"
    test "$(grep -Fc '"512x512"' "$imageProvider")" -eq 2
    mediaTool="$out/lib/openclaw/dist/openclaw-tools-dOabeT91.js"
    substituteInPlace "$mediaTool" \
      --replace-fail 'Size hint: 1024x1024, 1536x1024, 1024x1536, 2048x2048, 3840x2160.' 'Size hint: 512x512, 1024x1024, 1536x1024, 1024x1536, 2048x2048, 3840x2160.'
    substituteInPlace "$mediaTool" \
      --replace-fail 'return Boolean(normalizedSessionKey);' 'return Boolean(normalizedSessionKey) && process.env.NIXLOOM_OPENCLAW_SYNC_MEDIA !== "1";'
    test "$(grep -Fc 'NIXLOOM_OPENCLAW_SYNC_MEDIA !== "1"' "$mediaTool")" -eq 1
    controlUiServer="$out/lib/openclaw/dist/control-ui-mF5kFcwv.js"
    substituteInPlace "$controlUiServer" \
      --replace-fail 'import { r as authorizeHttpGatewayConnect } from "./auth-xp2ucrEC.js";' 'import { i as authorizeWsControlUiGatewayConnect } from "./auth-xp2ucrEC.js";'
    substituteInPlace "$controlUiServer" \
      --replace-fail 'let resolvedAuthResult = await authorizeHttpGatewayConnect({' 'let resolvedAuthResult = await authorizeWsControlUiGatewayConnect({'
    test "$(grep -Fc 'authorizeWsControlUiGatewayConnect({' "$controlUiServer")" -eq 1
    rm "$out/bin/openclaw"
    makeWrapper ${pkgs.nodejs-slim}/bin/node "$out/bin/openclaw" \
      --set NODE_PATH "$out/lib/openclaw/node_modules" \
      --add-flags "$out/lib/openclaw/dist/index.js"
  '';

  openclawUi = pkgs.stdenvNoCC.mkDerivation {
    pname = "nixloom-openclaw-ui";
    version = pkgs.openclaw.version;
    src = pkgs.openclaw.src;
    nativeBuildInputs = [ pkgs.nodejs-slim_22 ];
    doCheck = true;
    postPatch = ''
      rm -rf ui
      cp -r ${./openclaw-ui} ui
      chmod -R u+w ui
      # The packaged gateway already contains the exact dependency closure
      # with which it was built.  Reusing it avoids fetching every optional
      # cross-platform binary in OpenClaw's monorepo just to run Vite.
      cp -a --reflink=auto ${pkgs.openclaw}/lib/openclaw/node_modules ./node_modules
      chmod -R u+w node_modules
    '';
    buildPhase = ''
      runHook preBuild

      cd ui
      OPENCLAW_CONTROL_UI_BUILD_ID="nixloom-${pkgs.openclaw.version}" \
        node ../node_modules/vite/bin/vite.js build --config vite.config.ts

      runHook postBuild
    '';
    checkPhase = ''
      runHook preCheck

      cd "$NIX_BUILD_TOP/$sourceRoot/ui"
      node ../node_modules/vitest/vitest.mjs run --config vitest.config.ts \
        src/ui/chat/grouped-render.test.ts

      runHook postCheck
    '';
    installPhase = ''
      runHook preInstall
      mkdir -p "$out"
      cp -r ../dist/control-ui/. "$out/"
      test -f "$out/index.html"
      test -f "$out/manifest.webmanifest"
      runHook postInstall
    '';
  };

  openclawRuntime = pkgs.symlinkJoin {
    name = "nixloom-openclaw-runtime";
    paths = [ openclaw ];
    postBuild = ''
      mkdir -p "$out/share/nixloom"
      cp -r --no-preserve=mode ${yuanbao} "$out/share/nixloom/openclaw-plugin-yuanbao"
      ln -s ${openclaw}/lib/openclaw \
        "$out/share/nixloom/openclaw-plugin-yuanbao/node_modules/openclaw"
      ln -s ${tavily} "$out/share/nixloom/openclaw-plugin-tavily"
      cp -r --no-preserve=mode ${openclawUi} "$out/share/nixloom/openclaw-control-ui"
    '';
  };

  llamaCuda = pkgs.llama-cpp.override {
    cudaSupport = true;
    vulkanSupport = false;
  };
in
{
  default = nixloom;
  inherit nixloom;
  openclaw = openclawRuntime;
  sillytavern = pkgs.sillytavern;
  llama-swap = pkgs.llama-swap;
  llama-cpu = pkgs.llama-cpp;
  llama-cuda = llamaCuda;
  llama-vulkan = pkgs.llama-cpp-vulkan;
  image-cpu = pkgs.stable-diffusion-cpp;
  image-cuda = pkgs.stable-diffusion-cpp-cuda;
  image-vulkan = pkgs.stable-diffusion-cpp-vulkan;
}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isx86_64 {
  llama-rocm = pkgs.llama-cpp-rocm;
  image-rocm = pkgs.stable-diffusion-cpp-rocm;
}
