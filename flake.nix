{
  description = "NixLoom: a modular local-AI runtime for NixOS";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.home-manager = {
    url = "github:nix-community/home-manager";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
      ...
    }:
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
      architectureSource = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./README.md
          ./config.yaml
          ./flake.nix
          ./nix
          ./pyproject.toml
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
          openclaw =
            if lib.getVersion pkgs.openclaw == "2026.6.33" then
              pkgs.openclaw.overrideAttrs (_: {
                pnpmDepsHash = "sha256-rhfO66Nm5JDvozQAXC953QWbC9beUubg+Llykx59M/Q=";
              })
            else
              pkgs.openclaw;
        in
        pkgs.symlinkJoin {
          name = "nixloom-openclaw-runtime";
          paths = [ openclaw ];
          postBuild = ''
            mkdir -p "$out/share/nixloom"
            ln -s ${yuanbao} "$out/share/nixloom/openclaw-plugin-yuanbao"
          '';
        };
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
          sillytavern = pkgs.sillytavern;
          llama-swap = pkgs.llama-swap;
          llama-cpu = pkgs.llama-cpp;
          llama-cuda = llamaCuda;
          llama-vulkan = pkgs.llama-cpp-vulkan;
          image-cpu = pkgs.stable-diffusion-cpp;
          image-cuda = pkgs.stable-diffusion-cpp-cuda;
          image-vulkan = pkgs.stable-diffusion-cpp-vulkan;
        }
        // lib.optionalAttrs (system == "x86_64-linux") {
          llama-rocm = pkgs.llama-cpp-rocm;
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
                cd ${architectureSource}
                test -z "$(find . -name '*.sh' -print -quit)"
                if rg -i 'kobold[c]pp|open[[:space:]_-]*web[u]i|hermes[-_]?[a]gent|deployment\.frontends|100\.64\.0\.0' .; then
                  echo "legacy runtime or platform coupling remains" >&2
                  exit 1
                fi
                if rg 'ExecStart = .* service |"cmd": "nixloom service ' nix src; then
                  echo "public CLI service entry point is referenced internally" >&2
                  exit 1
                fi
                touch "$out"
              '';
          module-evaluation =
            let
              baseModule = {
                home.username = "nixloom-test";
                home.homeDirectory = "/home/nixloom-test";
                home.stateVersion = "26.05";
              };
              serviceNames =
                modules:
                builtins.attrNames
                  (home-manager.lib.homeManagerConfiguration {
                    inherit pkgs;
                    modules = [ baseModule ] ++ modules;
                  }).config.systemd.user.services;
              matrix = {
                core = serviceNames [
                  self.homeManagerModules.core
                  { services.nixloom.enable = true; }
                ];
                openclaw = serviceNames [
                  self.homeManagerModules.openclaw
                  {
                    services.nixloom.enable = true;
                    services.nixloom.openclaw.enable = true;
                  }
                ];
                sillytavern = serviceNames [
                  self.homeManagerModules.sillytavern
                  {
                    services.nixloom.enable = true;
                    services.nixloom.sillytavern.enable = true;
                  }
                ];
                complete = serviceNames [
                  self.homeManagerModules.default
                  {
                    services.nixloom.enable = true;
                    services.nixloom.images.enable = true;
                    services.nixloom.openclaw.enable = true;
                    services.nixloom.sillytavern.enable = true;
                  }
                ];
              };
              has = name: services: lib.elem name services;
            in
            assert has "nixloom-runtime" matrix.core;
            assert !(has "nixloom-openclaw" matrix.core);
            assert has "nixloom-openclaw" matrix.openclaw;
            assert !(has "nixloom-sillytavern" matrix.openclaw);
            assert has "nixloom-sillytavern" matrix.sillytavern;
            assert has "nixloom-openclaw" matrix.complete;
            assert has "nixloom-sillytavern" matrix.complete;
            pkgs.writeText "nixloom-module-matrix.json" (builtins.toJSON matrix);
          sillytavern-sdcpp-contract = pkgs.runCommand "nixloom-sillytavern-sdcpp-contract" { } ''
            grep -F "urlJoin(request.body.url, '/v1/models')" \
              ${pkgs.sillytavern.src}/src/endpoints/stable-diffusion.js >/dev/null
            grep -F "urlJoin(request.body.url, '/sdapi/v1/txt2img')" \
              ${pkgs.sillytavern.src}/src/endpoints/stable-diffusion.js >/dev/null
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
