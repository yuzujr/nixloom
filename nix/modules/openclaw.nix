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
    "OPENCLAW_STATE_DIR=${stateDir}/.openclaw"
    "OPENCLAW_CONFIG_PATH=${stateDir}/.openclaw/openclaw.json"
    "OPENCLAW_NIX_MODE=1"
    "NIXLOOM_OPENCLAW_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-yuanbao"
    "NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-tavily"
    "PATH=${lib.makeBinPath [ module.package ]}:${hostPath}"
  ];
  # The Control UI is served from a mutable state-dir copy (see
  # nixloom.openclaw._sync_control_ui).  prepare() mirrors the packaged
  # dist/control-ui there at activation so the CSS can be edited in place and
  # picked up by the phone without rebuilding the openclaw package.
  prepareEnvironment = lib.concatMapStringsSep " " lib.escapeShellArg [
    "NIXLOOM_STATE_DIR=${stateDir}"
    "NIXLOOM_DATA_DIR=${toString cfg.dataDir}"
    "NIXLOOM_CACHE_DIR=${toString cfg.cacheDir}"
    "NIXLOOM_CONFIG_FILE=${toString cfg.configFile}"
    "NIXLOOM_OPENCLAW_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-yuanbao"
    "NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-tavily"
    "NIXLOOM_OPENCLAW_CONTROL_UI_ROOT=${stateDir}/.openclaw/control-ui"
    "NIXLOOM_OPENCLAW_CONTROL_UI_SRC=${module.package}/lib/openclaw/dist/control-ui"
  ];
  launcher = pkgs.writeShellScript "nixloom-openclaw" ''
    set -eu
    export OPENCLAW_GATEWAY_TOKEN="$(<"$CREDENTIALS_DIRECTORY/gateway-token")"
    export TAVILY_API_KEY="$(<"$CREDENTIALS_DIRECTORY/tavily-api-key")"
    export YUANBAO_APP_KEY="$(<"$CREDENTIALS_DIRECTORY/yuanbao-app-key")"
    export YUANBAO_APP_SECRET="$(<"$CREDENTIALS_DIRECTORY/yuanbao-app-secret")"
    exec ${module.package}/bin/openclaw gateway
  '';
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
    home.activation.nixloomOpenClawConfig = lib.hm.dag.entryAfter [ "nixloomDirectories" ] ''
      run env ${prepareEnvironment} ${cfg.package}/bin/nixloom __service openclaw --prepare-only
    '';
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
        ExecStart = launcher;
        WorkingDirectory = stateDir;
        Environment = environment;
        LoadCredential = [
          "gateway-token:${stateDir}/.run/openclaw-gateway-token"
          "tavily-api-key:${stateDir}/.run/openclaw-tavily-api-key"
          "yuanbao-app-key:${stateDir}/.run/openclaw-yuanbao-app-key"
          "yuanbao-app-secret:${stateDir}/.run/openclaw-yuanbao-app-secret"
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
