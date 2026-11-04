{
    lib,
    pkgs,
    source,
    comfyui,
}:
let
    nixloom = pkgs.python3Packages.buildPythonApplication {
        pname = "nixloom";
        version = "0.2.0";
        src = source;
        pyproject = true;
        build-system = [ pkgs.python3Packages.setuptools ];
        dependencies = [
            pkgs.python3Packages.pyyaml
            pkgs.python3Packages.aiohttp
        ];
        nativeBuildInputs = [ pkgs.makeWrapper ];
        doCheck = false;
        postInstall = ''
            mkdir -p "$out/share/nixloom"
            cp config.yaml "$out/share/nixloom/config.yaml"
            "$CC" -shared -fPIC -O2 src/nixloom/llama_sleep.c -o "$out/share/nixloom/libllama_sleep.so" -ldl
        '';
        postFixup = ''
            wrapProgram "$out/bin/nixloom" \
              --set NIXLOOM_SHARE "$out/share/nixloom"
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
    inherit (pkgs) sillytavern;
    llama-cpu = pkgs.llama-cpp;
    llama-cuda = llamaCuda;
    llama-vulkan = pkgs.llama-cpp-vulkan;
    comfyui = pkgs.comfyui;
}
// lib.optionalAttrs pkgs.stdenv.hostPlatform.isx86_64 {
    comfyui-cuda = import ./comfyui.nix { inherit lib pkgs comfyui; };
    llama-rocm = pkgs.llama-cpp-rocm;
}
