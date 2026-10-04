{
    self,
    home-manager,
    lib,
    pkgs,
    source,
    architectureSource,
}:
{
    package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;

    python-lint = pkgs.runCommand "nixloom-python-lint" { nativeBuildInputs = [ pkgs.ruff ]; } ''
        cd ${source}
        export RUFF_CACHE_DIR="$TMPDIR/ruff-cache"
        ruff check src tests
        touch "$out"
    '';

    tavily-contract =
        pkgs.runCommand "nixloom-tavily-contract" { nativeBuildInputs = [ pkgs.nodejs_24 ]; }
            ''
                cd ${source}
                node --test tests/*.mjs
                touch "$out"
            '';

    architecture = pkgs.runCommand "nixloom-architecture" { nativeBuildInputs = [ pkgs.ripgrep ]; } ''
        cd ${architectureSource}
        test -z "$(find . -name '*.sh' -print -quit)"
        if rg -i 'open[c]law|kobold[c]pp|open[[:space:]_-]*web[u]i|hermes[-_]?[a]gent|deployment\.frontends|100\.64\.0\.0' .; then
          echo "legacy runtime or platform coupling remains" >&2
          exit 1
        fi
        public_service="ExecStart = .* ser"'vice |"cmd": "nixloom ser'"vice "
        if rg "$public_service" nix src; then
          echo "public CLI service entry point is referenced internally" >&2
          exit 1
        fi
        touch "$out"
    '';

    module-evaluation =
        let
            baseModule = {
                home = {
                    username = "nixloom-test";
                    homeDirectory = "/home/nixloom-test";
                    stateVersion = "26.05";
                };
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
                sillytavern = serviceNames [
                    self.homeManagerModules.sillytavern
                    {
                        services.nixloom = {
                            enable = true;
                            sillytavern.enable = true;
                        };
                    }
                ];
                dsh = serviceNames [
                    self.homeManagerModules.dsh
                    {
                        services.nixloom = {
                            enable = true;
                        };
                    }
                ];
                disabled = serviceNames [
                    self.homeManagerModules.default
                    {
                        services.nixloom = {
                            enable = true;
                            dsh.enable = false;
                        };
                    }
                ];
                complete = serviceNames [
                    self.homeManagerModules.default
                    {
                        services.nixloom = {
                            enable = true;
                            images.enable = true;
                            sillytavern.enable = true;
                        };
                    }
                ];
            };
            has = name: services: lib.elem name services;
            defaultConfig =
                (home-manager.lib.homeManagerConfiguration {
                    inherit pkgs;
                    modules = [
                        baseModule
                        self.homeManagerModules.default
                        { services.nixloom.enable = true; }
                    ];
                }).config;
        in
        assert has "nixloom-runtime" matrix.core;
        assert !(has "nixloom-dsh" matrix.core);
        assert has "nixloom-dsh" matrix.dsh;
        assert !(has "nixloom-sillytavern" matrix.dsh);
        assert has "nixloom-sillytavern" matrix.sillytavern;
        assert has "nixloom-dsh" matrix.complete;
        assert !(has "nixloom-dsh" matrix.disabled);
        assert has "nixloom-sillytavern" matrix.complete;
        assert defaultConfig.services.nixloom.dsh.enable;
        assert lib.elem "nixloomDirectories" defaultConfig.home.activation.nixloomDshInstall.after;
        assert lib.hasInfix "__service dsh --prepare-only"
            defaultConfig.home.activation.nixloomDshInstall.data;
        pkgs.writeText "nixloom-module-matrix.json" (builtins.toJSON matrix);

}
