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

  architecture = pkgs.runCommand "nixloom-architecture" { nativeBuildInputs = [ pkgs.ripgrep ]; } ''
    cd ${architectureSource}
    test -z "$(find . -name '*.sh' -print -quit)"
    if rg -i 'kobold[c]pp|open[[:space:]_-]*web[u]i|hermes[-_]?[a]gent|deployment\.frontends|100\.64\.0\.0' .; then
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
