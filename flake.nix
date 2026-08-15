{
  description = "NixLoom: a reproducible local AI runtime for NixOS";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.hermes-agent.url = "github:NousResearch/hermes-agent";

  outputs = { self, nixpkgs, hermes-agent, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };
      llamaVulkan = pkgs.llama-cpp-vulkan;
      llamaCuda = pkgs.llama-cpp.override {
        cudaSupport = true;
        vulkanSupport = false;
      };
      hermesPackage = hermes-agent.packages.${system}.default;
      # open-webui propagates a Python 3.14 PYTHONPATH into mkShell, while
      # Hermes is built with Python 3.12. Give Hermes its own DDGS/lxml path
      # and remove it from the environment after interpreter startup so tool
      # subprocesses do not inherit a foreign Python environment.
      # Follow the interpreter hermes-agent was built with when the flake
      # exposes it, so a Python bump upstream cannot silently mismatch the
      # injected site-packages (lxml is a C extension; an ABI mismatch only
      # fails at runtime).
      hermesPython = hermesPackage.passthru.python or pkgs.python312;
      hermesWebDeps = hermesPython.withPackages (ps: [
        ps.ddgs
        ps.lxml
      ]);
      hermesPythonCleanup = pkgs.writeTextDir "${hermesPython.sitePackages}/sitecustomize.py" ''
        import os
        import sys
        os.environ.pop("PYTHONPATH", None)
        # Upstream's uv2nix environment exposes python3.12 but currently sets
        # HERMES_PYTHON to a non-existent bin/python3 path.
        os.environ["HERMES_PYTHON"] = sys.executable
      '';
      hermesIsolated = pkgs.symlinkJoin {
        name = "hermes-agent-isolated";
        paths = [ hermesPackage ];
        nativeBuildInputs = [ pkgs.makeWrapper ];
        postBuild = ''
          for command in hermes hermes-agent hermes-acp; do
            if [[ -x "$out/bin/$command" ]]; then
              wrapProgram "$out/bin/$command" \
                --set PYTHONNOUSERSITE 1 \
                --set PYTHONPATH "${hermesPythonCleanup}/${hermesPython.sitePackages}:${hermesWebDeps}/${hermesPython.sitePackages}"
            fi
          done
        '';
      };
      # Two nixpkgs 1.110 packaging fixes needed for image generation:
      # embd_res/ (CLIP tokenizer data, read from bin/embd_res via realpath)
      # is not installed — a backend-independent bug, so it is fixed for every
      # koboldcpp variant — and koboldcpp_cublas.so uses driver-API symbols
      # (cuMemCreate) without linking libcuda, which only exists at the NixOS
      # driver path at runtime.
      koboldFixEmbdRes = kobold: kobold.overrideAttrs (old: {
        postInstall = (old.postInstall or "") + ''
          mkdir -p $out/bin/embd_res
          cp -r embd_res/. $out/bin/embd_res/
        '';
      });
      koboldVulkan = koboldFixEmbdRes pkgs.koboldcpp;
      koboldCuda = (koboldFixEmbdRes (pkgs.koboldcpp.override { cublasSupport = true; })).overrideAttrs (old: {
        nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.patchelf ];
        postFixup = (old.postFixup or "") + ''
          patchelf --add-needed /run/opengl-driver/lib/libcuda.so.1 \
            $out/bin/koboldcpp_cublas.so
        '';
      });
      # SillyTavern 1.18.0 overwrites url.pathname when calling A1111 image
      # endpoints, dropping base paths like llama-swap's /upstream/sd.
      # Prefix the original path instead so the swap proxy keeps working.
      # Backport of upstream PR #5427 (merged 2026-04, fixes issue #4411);
      # drop this override once nixpkgs ships a release containing it.
      sillytavernPatched = pkgs.sillytavern.overrideAttrs (old: {
        postPatch = (old.postPatch or "") + ''
          sed -i "s|\([A-Za-z0-9_]*\)\.pathname = '/sdapi|\1.pathname = \1.pathname.replace(/[/]+\$/, \"\") + '/sdapi|g" \
            src/endpoints/stable-diffusion.js
          if grep -q "\.pathname = '/sdapi" src/endpoints/stable-diffusion.js; then
            echo "unpatched sdapi path assignments remain" >&2
            exit 1
          fi
        '';
      });
      commonPackages = [
        pkgs.bubblewrap
        pkgs.curl
        pkgs.coreutils
        pkgs.findutils
        pkgs.gnutar
        pkgs.gzip
        pkgs.jq
        pkgs.procps
        pkgs.python3
        pkgs.systemd
        pkgs.util-linux
        pkgs.yq-go
        pkgs.llama-swap
        pkgs.open-webui
        sillytavernPatched
        hermesIsolated
      ];
      runtimePackages = [ llamaCuda koboldCuda ] ++ commonPackages;
      source = builtins.path {
        path = ./.;
        name = "nixloom-source";
        filter = path: type:
          let
            root = toString ./.;
            value = toString path;
            relative = nixpkgs.lib.removePrefix "${root}/" value;
            included = [ "bin" "config" "scripts" "tests" ];
          in
          value == root
          || relative == ".env.example"
          || relative == "config.yaml"
          || builtins.any (prefix:
            relative == prefix || nixpkgs.lib.hasPrefix "${prefix}/" relative
          ) included;
      };
      nixloom = pkgs.stdenvNoCC.mkDerivation {
        pname = "nixloom";
        version = "0.1.0";
        src = source;
        nativeBuildInputs = [ pkgs.makeWrapper ];
        installPhase = ''
          runHook preInstall
          mkdir -p "$out/bin" "$out/libexec/nixloom" "$out/share/nixloom"
          cp -r bin config scripts tests config.yaml "$out/libexec/nixloom/"
          cp config.yaml config/models.lock.yaml .env.example "$out/share/nixloom/"
          chmod +x "$out/libexec/nixloom/bin/nixloom" \
            "$out/libexec/nixloom/scripts/"*.sh \
            "$out/libexec/nixloom/tests/"*.sh \
            "$out/libexec/nixloom/tests/benchmark.py"
          patchShebangs "$out/libexec/nixloom"
          makeWrapper "$out/libexec/nixloom/bin/nixloom" "$out/bin/nixloom" \
            --set NIXLOOM_LIBEXEC "$out/libexec/nixloom" \
            --set NIXLOOM_SHARE "$out/share/nixloom" \
            --prefix PATH : "${pkgs.lib.makeBinPath runtimePackages}" \
            --prefix LD_LIBRARY_PATH : /run/opengl-driver/lib
          runHook postInstall
        '';
      };
      shellHook = backend: ''
        export NIXLOOM_ROOT="$PWD"
        export HF_HOME="$PWD/.hf"
        export XDG_CACHE_HOME="$PWD/.cache"
        export LD_LIBRARY_PATH="/run/opengl-driver/lib:$LD_LIBRARY_PATH"

        echo "NixLoom ${backend} development shell ready."
        echo "Run nixloom --help for the supported interface."
      '';
    in
    {
      # GPU integration checks are intentionally manual; `nix flake check`
      # remains deterministic and only lints scripts and config consistency.
      checks.${system} = {
        # Globbed so a new script cannot silently escape linting.
        shellcheck = pkgs.runCommand "shellcheck" {
          nativeBuildInputs = [ pkgs.shellcheck ];
        } ''
          cd ${self}
          shellcheck --severity=warning -x \
            bin/nixloom scripts/*.sh config/*.sh tests/*.sh
          touch $out
        '';

        python-lint = pkgs.runCommand "python-lint" {
          nativeBuildInputs = [ pkgs.python3 ];
        } ''
          cd ${self}
          PYTHONPYCACHEPREFIX=$TMPDIR python3 -m py_compile scripts/*.py tests/*.py
          touch $out
        '';

        config-contract = pkgs.runCommand "config-contract" {
          nativeBuildInputs = [ pkgs.bash pkgs.jq pkgs.yq-go pkgs.python3 ];
        } ''
          cp -r ${self} source
          chmod -R u+w source
          cd source
          patchShebangs bin scripts tests
          export TAVILY_API_KEY=test
          export SILLYTAVERN_AUTH_PASSWORD=test
          export HERMES_API_KEY=test
          NIXLOOM_CONFIG_FILE="$PWD/config.yaml" bash -c \
            'source config/lib.sh; check_config_keys'
          ./scripts/llama.sh --dry-run >/dev/null
          ./scripts/swap.sh --dry-run >/dev/null
          NIXLOOM_ROOT="$PWD" ./bin/nixloom config check >/dev/null
          touch $out
        '';

        # A normal installation has only the immutable package and an empty
        # writable state directory. It must not depend on a source checkout,
        # copied config or pre-existing .env.
        clone-free-install = pkgs.runCommand "clone-free-install" {
          nativeBuildInputs = [ pkgs.gnugrep ];
        } ''
          mkdir state
          export NIXLOOM_ROOT="$PWD/state"
          ${nixloom}/bin/nixloom config check 2>config.stderr
          grep -q 'web search is disabled' config.stderr
          test ! -e state/config.yaml
          test -r ${nixloom}/share/nixloom/config.yaml
          test -r ${nixloom}/share/nixloom/models.lock.yaml
          ${nixloom}/bin/nixloom env init --yes
          test -f state/.env
          test "$(stat -c '%a' state/.env)" = 600
          if ${nixloom}/bin/nixloom models check qwen36_mmproj \
            >models.stdout 2>models.stderr; then
            echo "an empty state directory unexpectedly passed model verification" >&2
            exit 1
          fi
          grep -q 'missing or invalid.*qwen36_mmproj' models.stderr
          touch $out
        '';

        # Config and the asset lock must describe exactly the same files.
        model-paths = pkgs.runCommand "model-paths" {
          nativeBuildInputs = [ pkgs.yq-go ];
        } ''
          cd ${self}
          lock_paths="$({ yq -r '.assets[] | .path' config/models.lock.yaml; } | sort -u)"
          cfg_paths="$({
            yq -r '.llm.model_file' config.yaml
            yq -r '.llm.mmproj_file' config.yaml
            yq -r '.images.profiles[] | .model_file' config.yaml
            yq -r '.images.profiles[] | .lora' config.yaml
          } | grep -vx 'null' | grep -v '^$' | sort -u)"
          if [[ "$cfg_paths" != "$lock_paths" ]]; then
            echo "config.yaml and config/models.lock.yaml asset paths differ" >&2
            diff -u <(printf '%s\n' "$cfg_paths") <(printf '%s\n' "$lock_paths") >&2 || true
            exit 1
          fi
          touch $out
        '';

        # Keep the publishable source free of host identity and runtime data.
        # This guards against a future edit accidentally committing the
        # developer's absolute home path, Tailnet host, secrets, databases or
        # model weights.
        public-tree = pkgs.runCommand "public-tree" {
          nativeBuildInputs = [ pkgs.ripgrep pkgs.yq-go ];
        } ''
          cd ${self}
          if rg -n --hidden \
            '(/home/[[:alnum:]_.-]+/|[[:alnum:]-]+\.tail[[:xdigit:]]+\.ts\.net|100\.(6[4-9]|[78][0-9]|9[0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[1-9][0-9]{0,2})' \
            .; then
            echo "publishable source contains a host-specific path or address" >&2
            exit 1
          fi
          for private in .env .webui_secret_key; do
            if [[ -e "$private" ]]; then
              echo "publishable source contains private file: $private" >&2
              exit 1
            fi
          done
          if find . -maxdepth 1 -type f -name '.env.*' \
            ! -name '.env.example' -print -quit | grep -q .; then
            echo "publishable source contains an unrecognized environment file" >&2
            exit 1
          fi
          if find . -type f \( \
            -name '*.gguf' -o -name '*.safetensors' -o -name '*.db' \
          \) -print -quit | grep -q .; then
            echo "publishable source contains a model weight or database" >&2
            exit 1
          fi
          if [[ "$(yq -r '.deployment.remote' config.yaml)" != false ]]; then
            echo "the committed deployment must default to loopback-only" >&2
            exit 1
          fi
          touch $out
        '';
      };

      packages.${system}.default = nixloom;
      apps.${system}.default = {
        type = "app";
        program = "${nixloom}/bin/nixloom";
        meta.description = "Control the NixLoom local AI runtime";
      };
      homeManagerModules.default = { lib, pkgs, ... }: {
        imports = [ ./nix/home-manager.nix ];
        services.nixloom.package = lib.mkDefault
          self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };

      devShells.${system} = {
        default = pkgs.mkShell {
          packages = [ nixloom ] ++ runtimePackages;
          shellHook = shellHook "CUDA";
        };

        vulkan = pkgs.mkShell {
          packages = [ nixloom llamaVulkan koboldVulkan pkgs.vulkan-tools ] ++ commonPackages;
          shellHook = shellHook "Vulkan";
        };

        cuda = pkgs.mkShell {
          packages = [ nixloom ] ++ runtimePackages;
          shellHook = shellHook "CUDA";
        };
      };
    };
}
