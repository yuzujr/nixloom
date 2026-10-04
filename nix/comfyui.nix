{
    lib,
    pkgs,
    comfyui,
}:
let
    base = pkgs.comfyui;
    upstream = import comfyui.inputs.nixpkgs {
        system = pkgs.stdenv.hostPlatform.system;
        config.allowUnfree = true;
    };
    versions = import "${comfyui}/nix/versions.nix";
    nativeWheels = builtins.fromJSON (builtins.readFile ./python-wheels.json);
    # Keep the community pin and default architectures so CUDA cache entries match.
    # NVSHMEM is not cached at this pin; NVIDIA's wheel avoids compiling its tests.
    nvshmem = upstream.stdenv.mkDerivation {
        pname = "libnvshmem-bin";
        version = "3.6.5";
        src = upstream.fetchurl {
            url = "https://files.pythonhosted.org/packages/5d/7b/2ab033584a3339552472ac8d79543c503a0e06dd0d082448b06697e7f716/nvidia_nvshmem_cu13-3.6.5-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl";
            hash = "sha256-QAGqvHLq0y7MPJrdPGeBvvy3Gty+KG1/WVYELmhmjHA=";
        };
        nativeBuildInputs = [
            upstream.unzip
            upstream.autoPatchelfHook
        ];
        buildInputs = [
            upstream.stdenv.cc.cc.lib
            upstream.rdma-core
            upstream.libfabric
            upstream.ucx
            upstream.openmpi
            upstream.pmix
        ];
        sourceRoot = ".";
        unpackPhase = ''
            runHook preUnpack
            unzip -q "$src"
            runHook postUnpack
        '';
        dontConfigure = true;
        dontBuild = true;
        installPhase = ''
            runHook preInstall
            mkdir -p "$out"
            cp -r nvidia/nvshmem/* "$out/"
            mkdir -p "$out/share/licenses"
            cp nvidia_nvshmem_cu13-*.dist-info/licenses/License.txt "$out/share/licenses/"
            runHook postInstall
        '';
        autoPatchelfIgnoreMissingDeps = [ "libcuda.so.1" ];
        meta = {
            description = "NVIDIA's prebuilt NVSHMEM runtime";
            license = lib.licenses.unfree;
            platforms = [ "x86_64-linux" ];
        };
    };
    wheelPkgs = upstream // {
        cudaPackages_13 = upstream.cudaPackages_13.overrideScope (
            _final: _prev: {
                libnvshmem = nvshmem;
            }
        );
    };
    python = upstream.python312.override {
        packageOverrides =
            lib.composeExtensions
                (import "${comfyui}/nix/python-overrides.nix" {
                    pkgs = wheelPkgs;
                    inherit versions;
                    gpuSupport = "cuda";
                })
                (
                    final: prev:
                    {
                        # Its upstream notebook tests pull in Jupyter and Node; keep the import check.
                        einops = prev.einops.overridePythonAttrs {
                            doCheck = false;
                            nativeCheckInputs = [ ];
                        };
                    }
                    // lib.mapAttrs (
                        name: spec:
                        final.buildPythonPackage {
                            pname = name;
                            inherit (spec) version;
                            format = "wheel";
                            src = upstream.fetchurl { inherit (spec) url hash; };
                            nativeBuildInputs = [ upstream.autoPatchelfHook ];
                            buildInputs = [ upstream.stdenv.cc.cc.lib ];
                            dependencies = lib.optional (name == "tokenizers") final.huggingface-hub;
                            doCheck = false;
                            pythonImportsCheck = [ (lib.replaceStrings [ "-" ] [ "_" ] name) ];
                            meta = prev.${name}.meta // {
                                platforms = [ "x86_64-linux" ];
                            };
                        }
                    ) nativeWheels
                );
    };
    vendored = import "${comfyui}/nix/vendored-packages.nix" {
        pkgs = wheelPkgs;
        inherit python versions;
    };
    pythonEnv = python.withPackages (
        ps:
        (with ps; [
            aiohttp
            alembic
            av
            blake3
            einops
            filelock
            kornia
            numpy
            pillow
            psutil
            pydantic
            pydantic-settings
            pyopengl
            pyyaml
            requests
            safetensors
            scipy
            sentencepiece
            simpleeval
            spandrel
            sqlalchemy
            tokenizers
            torch
            torchaudio
            torchsde
            torchvision
            tqdm
            transformers
            yarl
        ])
        ++ [
            vendored.comfyKitchen
            vendored.comfyAimdo
            vendored.comfyAngle
            vendored.comfyuiFrontendPackage
            vendored.comfyuiWorkflowTemplates
            vendored.comfyuiEmbeddedDocs
        ]
    );
in
base.overrideAttrs (old: {
    installPhase =
        lib.replaceStrings
            [ (lib.getExe base.pythonEnv) (toString base.pythonEnv) "--unset PYTHONPATH" ]
            [
                (lib.getExe pythonEnv)
                (toString pythonEnv)
                ''--unset PYTHONPATH --prefix LD_LIBRARY_PATH : "${
                    lib.makeLibraryPath [
                        wheelPkgs.cudaPackages_13.cuda_nvrtc
                        wheelPkgs.cudaPackages_13.cuda_cudart
                        wheelPkgs.cudaPackages_13.libcublas
                    ]
                }"''
            ]
            old.installPhase;
    installCheckPhase =
        lib.replaceStrings
            [ (toString base.pythonEnv) base.python.sitePackages (toString base.python.pkgs.pip) ]
            [ (toString pythonEnv) python.sitePackages (toString python.pkgs.pip) ]
            old.installCheckPhase;
    passthru = old.passthru // {
        inherit python pythonEnv nvshmem;
    };
})
