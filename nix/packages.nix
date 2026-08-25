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
  # The Control UI polish layer lives outside the OpenClaw source tree as a
  # CSS file injected into the package's own bundled dist/control-ui at build
  # time.  The Gateway serves that bundled root on its package-proven path
  # (rejectHardlinks is disabled for it), so no fork or Vite rebuild is needed
  # and the Nix store's hard-link dedup does not break serving.
  openclaw = pkgs.openclaw.overrideAttrs (old: {
    postInstall = (old.postInstall or "") + ''
      controlUi="$out/lib/openclaw/dist/control-ui"
      substituteInPlace "$controlUi/index.html" \
        --replace-fail \
          '<link rel="manifest" href="./manifest.webmanifest" />' \
          '<link rel="manifest" href="./manifest.webmanifest" />
      <link rel="stylesheet" href="./nixloom-control-ui.css" />'
      cp ${./openclaw-control-ui.css} "$controlUi/nixloom-control-ui.css"
    '';
  });

  openclawRuntime = pkgs.symlinkJoin {
    name = "nixloom-openclaw-runtime";
    paths = [ openclaw ];
    postBuild = ''
      mkdir -p "$out/share/nixloom"
      cp -r --no-preserve=mode ${yuanbao} "$out/share/nixloom/openclaw-plugin-yuanbao"
      ln -s ${openclaw}/lib/openclaw \
        "$out/share/nixloom/openclaw-plugin-yuanbao/node_modules/openclaw"
      ln -s ${tavily} "$out/share/nixloom/openclaw-plugin-tavily"
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
