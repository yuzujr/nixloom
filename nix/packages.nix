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
        nativeCheckInputs = [
            pkgs.python3Packages.pyyaml
            pkgs.patch
        ];
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

    llamaCuda = pkgs.llama-cpp.override {
        cudaSupport = true;
        vulkanSupport = false;
    };
in
{
    default = nixloom;
    inherit nixloom;
    inherit (pkgs) sillytavern llama-swap;
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
