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
    # NixLoom owns local GPU scheduling and delivers media in the visible
    # reply, so media tools must finish in the originating agent turn.
    "NIXLOOM_OPENCLAW_SYNC_MEDIA=1"
    "NIXLOOM_OPENCLAW_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-yuanbao"
    "NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-tavily"
    "PATH=${lib.makeBinPath [ module.package ]}:${hostPath}"
  ];
  # The gateway requires a non-hardlinked Control UI root.  prepare() mirrors
  # NixLoom's built, owned UI there at activation.
  prepareEnvironment = lib.concatMapStringsSep " " lib.escapeShellArg [
    "NIXLOOM_STATE_DIR=${stateDir}"
    "NIXLOOM_DATA_DIR=${toString cfg.dataDir}"
    "NIXLOOM_CACHE_DIR=${toString cfg.cacheDir}"
    "NIXLOOM_CONFIG_FILE=${toString cfg.configFile}"
    "NIXLOOM_OPENCLAW_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-yuanbao"
    "NIXLOOM_OPENCLAW_TAVILY_PLUGIN_PATH=${module.package}/share/nixloom/openclaw-plugin-tavily"
    "NIXLOOM_OPENCLAW_CONTROL_UI_ROOT=${stateDir}/.openclaw/control-ui"
    "NIXLOOM_OPENCLAW_CONTROL_UI_SRC=${module.package}/share/nixloom/openclaw-control-ui"
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
    home.sessionVariables.NIXLOOM_OPENCLAW_SYNC_MEDIA = "1";
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
        # The gateway binary, packaged Control UI, and prepared config must
        # advance together.  Keeping the old process would serve new static
        # assets against an old gateway closure after a Home Manager switch.
        X-SwitchMethod = "restart";
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
        # OpenClaw's gateway performs its own "full process restart" (supervisor
        # restart) when the live config changes, and the restarted main process
        # then exits cleanly (status 0).  Under "on-failure" that clean exit is
        # treated as a normal stop and the unit is left dead; "always" makes
        # systemd the supervisor so the gateway comes back with the new config.
        Restart = "always";
        RestartSec = "5s";
        TimeoutStartSec = "infinity";
        TimeoutStopSec = "30s";
        KillMode = "mixed";
      };
      Install.WantedBy = [ "nixloom.target" ];
    };
  };
}
