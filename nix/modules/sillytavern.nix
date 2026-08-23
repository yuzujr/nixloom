{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.nixloom;
  module = cfg.sillytavern;
  stateDir = toString cfg.stateDir;
in
{
  options.services.nixloom.sillytavern = {
    enable = lib.mkEnableOption "the independent NixLoom SillyTavern module";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.sillytavern;
      description = "SillyTavern package.";
    };
  };

  config = lib.mkIf (cfg.enable && module.enable) {
    home.packages = [ module.package ];
    systemd.user.targets.nixloom.Unit.Wants = lib.mkAfter [ "nixloom-sillytavern.service" ];
    systemd.user.services.nixloom-sillytavern = {
      Unit = {
        Description = "NixLoom SillyTavern";
        Wants = [ "nixloom-runtime.service" ];
        After = [ "nixloom-runtime.service" ];
        PartOf = [ "nixloom.target" ];
        X-SwitchMethod = "keep-old";
      };
      Service = {
        ExecStart = "${cfg.package}/bin/nixloom __service sillytavern";
        WorkingDirectory = stateDir;
        Environment = [
          "NIXLOOM_STATE_DIR=${stateDir}"
          "NIXLOOM_DATA_DIR=${toString cfg.dataDir}"
          "NIXLOOM_CACHE_DIR=${toString cfg.cacheDir}"
          "NIXLOOM_CONFIG_FILE=${toString cfg.configFile}"
          "PATH=${lib.makeBinPath [ module.package ]}"
        ];
        Restart = "on-failure";
        RestartSec = "5s";
        TimeoutStartSec = "infinity";
        TimeoutStopSec = "30s";
        KillMode = "mixed";
      };
      Install.WantedBy = [ "nixloom.target" ];
    };
  };
}
