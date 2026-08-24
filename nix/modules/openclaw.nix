{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.nixloom;
  module = cfg.openclaw;
  stateDir = toString cfg.stateDir;
  hostPath = lib.concatStringsSep ":" [
    "/run/wrappers/bin"
    "${config.home.profileDirectory}/bin"
    "/run/current-system/sw/bin"
  ];
  environment = [
    "NIXLOOM_STATE_DIR=${stateDir}"
    "NIXLOOM_DATA_DIR=${toString cfg.dataDir}"
    "NIXLOOM_CACHE_DIR=${toString cfg.cacheDir}"
    "NIXLOOM_CONFIG_FILE=${toString cfg.configFile}"
    "NIXLOOM_OPENCLAW_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-yuanbao"
    "NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-tavily"
    "PATH=${lib.makeBinPath [ module.package ]}:${hostPath}"
  ];
in
{
  options.services.nixloom.openclaw = {
    enable = lib.mkEnableOption "the independent NixLoom OpenClaw module";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.openclaw;
      description = "OpenClaw plus the packaged Yuanbao and Tavily plugins.";
    };
  };

  config = lib.mkIf (cfg.enable && module.enable) {
    home.packages = [ module.package ];
    home.sessionVariables.NIXLOOM_OPENCLAW_PLUGIN_PATH = "${module.package}/share/nixloom/openclaw-plugin-yuanbao";
    home.sessionVariables.NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH = "${module.package}/share/nixloom/openclaw-plugin-tavily";
    systemd.user.targets.nixloom.Unit.Wants = lib.mkAfter [ "nixloom-openclaw.service" ];
    systemd.user.services.nixloom-openclaw = {
      Unit = {
        Description = "NixLoom OpenClaw agent gateway";
        Wants = [ "nixloom-runtime.service" ];
        After = [ "nixloom-runtime.service" ];
        PartOf = [ "nixloom.target" ];
        X-SwitchMethod = "keep-old";
      };
      Service = {
        ExecStart = "${cfg.package}/bin/nixloom __service openclaw";
        WorkingDirectory = stateDir;
        Environment = environment;
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
