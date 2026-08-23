{
  description = "NixLoom: a modular local-AI runtime for NixOS";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs, ... }:
    let
      lib = nixpkgs.lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = lib.genAttrs systems;
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config = {
            allowUnfree = true;
            allowInsecurePredicate = pkg: lib.getName pkg == "openclaw";
          };
        };
      source = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./pyproject.toml
          ./config.yaml
          ./src
          ./tests
        ];
      };
      mkNixloom =
        pkgs:
        pkgs.python3Packages.buildPythonApplication {
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
      mkYuanbaoPlugin =
        pkgs:
        pkgs.buildNpmPackage {
          pname = "openclaw-plugin-yuanbao";
          version = "2.18.2";
          src = ./nix/openclaw-yuanbao;
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
      mkOpenclaw =
        pkgs:
        let
          yuanbao = mkYuanbaoPlugin pkgs;
          # nixpkgs 2026.6.33 still carries the previous fixed-output hash.
          openclaw = pkgs.openclaw.overrideAttrs (_: {
            pnpmDepsHash = "sha256-rhfO66Nm5JDvozQAXC953QWbC9beUubg+Llykx59M/Q=";
          });
        in
        pkgs.symlinkJoin {
          name = "nixloom-openclaw-runtime";
          paths = [ openclaw ];
          postBuild = ''
            mkdir -p "$out/share/nixloom"
            ln -s ${yuanbao} "$out/share/nixloom/openclaw-plugin-yuanbao"
          '';
        };
      mkSillyTavern =
        pkgs:
        pkgs.sillytavern.overrideAttrs (old: {
          # SillyTavern 1.18 drops proxy base paths for A1111-compatible calls.
          # Its sdcpp integration still uses those calls for generation, so keep
          # the /upstream/sd prefix until the upstream fix reaches nixpkgs.
          postPatch = (old.postPatch or "") + ''
            sed -i "s|\([A-Za-z0-9_]*\)\.pathname = '/sdapi|\1.pathname = \1.pathname.replace(/[/]+\$/, \"\") + '/sdapi|g" \
              src/endpoints/stable-diffusion.js
            if grep -q "\.pathname = '/sdapi" src/endpoints/stable-diffusion.js; then
              echo "unpatched sdapi path assignments remain" >&2
              exit 1
            fi
          '';
        });
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          llamaCuda = pkgs.llama-cpp.override {
            cudaSupport = true;
            vulkanSupport = false;
          };
        in
        {
          default = mkNixloom pkgs;
          nixloom = mkNixloom pkgs;
          openclaw = mkOpenclaw pkgs;
          sillytavern = mkSillyTavern pkgs;
          llama-swap = pkgs.llama-swap;
          llama-cpu = pkgs.llama-cpp;
          llama-cuda = llamaCuda;
          llama-vulkan = pkgs.llama-cpp-vulkan;
          llama-rocm = pkgs.llama-cpp-rocm;
          image-cpu = pkgs.stable-diffusion-cpp;
          image-cuda = pkgs.stable-diffusion-cpp-cuda;
          image-vulkan = pkgs.stable-diffusion-cpp-vulkan;
          image-rocm = pkgs.stable-diffusion-cpp-rocm;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nixloom";
        };
      });

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          package = self.packages.${system}.default;
          python-lint =
            pkgs.runCommand "nixloom-python-lint"
              {
                nativeBuildInputs = [ pkgs.ruff ];
              }
              ''
                cd ${source}
                export RUFF_CACHE_DIR="$TMPDIR/ruff-cache"
                ruff check src tests
                touch "$out"
              '';
          architecture =
            pkgs.runCommand "nixloom-architecture"
              {
                nativeBuildInputs = [ pkgs.ripgrep ];
              }
              ''
                cd ${source}
                test -z "$(find . -name '*.sh' -print -quit)"
                if rg -i 'koboldcpp|open[ -]webui|hermes-agent|deployment\.frontends|100\.64\.0\.0' .; then
                  echo "legacy runtime or platform coupling remains" >&2
                  exit 1
                fi
                touch "$out"
              '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python3.withPackages (packages: [ packages.pyyaml ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.nixfmt
              pkgs.ruff
            ];
            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
            '';
          };
        }
      );

      homeManagerModules =
        let
          core = import ./nix/modules/core.nix { inherit self nixpkgs; };
          openclaw = import ./nix/modules/openclaw.nix { inherit self; };
          sillytavern = import ./nix/modules/sillytavern.nix { inherit self; };
        in
        {
          inherit core;
          openclaw = {
            imports = [
              core
              openclaw
            ];
          };
          sillytavern = {
            imports = [
              core
              sillytavern
            ];
          };
          default = {
            imports = [
              core
              openclaw
              sillytavern
            ];
          };
        };
    };
}
