{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.nixloom;
  stateDir = toString cfg.stateDir;
  configFile = toString cfg.configFile;
  assetLockFile = toString cfg.assetLockFile;
  command = "${cfg.package}/bin/nixloom";
  commonUnit = {
    PartOf = [ "nixloom.target" ];

    # Home Manager normally restarts changed user services during activation.
    # Keep the current model/runtime alive until an explicit `nixloom restart`.
    X-SwitchMethod = "keep-old";
  };
  commonService = {
    WorkingDirectory = stateDir;
    Environment = [
      "NIXLOOM_ROOT=${stateDir}"
      "NIXLOOM_CONFIG_FILE=${configFile}"
      "NIXLOOM_ASSET_LOCK_FILE=${assetLockFile}"
      "LD_LIBRARY_PATH=/run/opengl-driver/lib"
    ];
    Restart = "on-failure";
    RestartSec = "5s";
    TimeoutStartSec = "infinity";
    TimeoutStopSec = "30s";
    KillMode = "mixed";
  };
  frontend = name: description: {
    Unit = commonUnit // {
      Description = description;
      Requires = [ "nixloom-runtime.service" ];
      After = [ "nixloom-runtime.service" ];
    };
    Service = commonService // {
      ExecCondition = "${command} __enabled ${name}";
      ExecStart = "${command} __service ${name}";
    };
    Install.WantedBy = [ "nixloom.target" ];
  };
in
{
  options.services.nixloom = {
    enable = lib.mkEnableOption "the NixLoom local AI runtime";
    package = lib.mkOption {
      type = lib.types.package;
      description = "NixLoom package to run.";
    };
    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.dataHome}/nixloom";
      defaultText = lib.literalExpression ''"${config.xdg.dataHome}/nixloom"'';
      description = "Absolute writable directory for models, databases, caches and .env.";
    };
    configFile = lib.mkOption {
      type = lib.types.either lib.types.path lib.types.str;
      default = cfg.package + "/share/nixloom/config.yaml";
      defaultText = lib.literalExpression
        ''config.services.nixloom.package + "/share/nixloom/config.yaml"'';
      description = "Runtime YAML configuration; defaults to the immutable packaged profile.";
    };
    assetLockFile = lib.mkOption {
      type = lib.types.either lib.types.path lib.types.str;
      default = cfg.package + "/share/nixloom/models.lock.yaml";
      defaultText = lib.literalExpression
        ''config.services.nixloom.package + "/share/nixloom/models.lock.yaml"'';
      description = "Pinned asset manifest; defaults to the immutable packaged lock.";
    };
    autoStart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Start NixLoom at login. Disabled to avoid surprise model loading.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasPrefix "/" stateDir;
        message = "services.nixloom.stateDir must be an absolute path";
      }
      {
        assertion = lib.hasPrefix "/" configFile;
        message = "services.nixloom.configFile must be an absolute path";
      }
      {
        assertion = lib.hasPrefix "/" assetLockFile;
        message = "services.nixloom.assetLockFile must be an absolute path";
      }
    ];
    home.packages = [ cfg.package ];
    home.sessionVariables = {
      NIXLOOM_ROOT = stateDir;
      NIXLOOM_CONFIG_FILE = configFile;
      NIXLOOM_ASSET_LOCK_FILE = assetLockFile;
    };
    home.activation.nixloomStateDirectory =
      lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        run mkdir -p ${lib.escapeShellArg stateDir}
        run chmod 700 ${lib.escapeShellArg stateDir}
      '';

    systemd.user.targets.nixloom = {
      Unit = {
        Description = "NixLoom local AI runtime";
        Wants = [
          "nixloom-runtime.service"
          "nixloom-hermes.service"
          "nixloom-webui.service"
          "nixloom-sillytavern.service"
        ];
        After = [ "nixloom-runtime.service" ];
      };
      Install.WantedBy = lib.optional cfg.autoStart "default.target";
    };

    systemd.user.services.nixloom-runtime = {
      Unit = commonUnit // {
        Description = "NixLoom model and image swap runtime";
      };
      Service = commonService // {
        ExecStart = "${command} __service runtime";
      };
      Install.WantedBy = [ "nixloom.target" ];
    };

    systemd.user.services.nixloom-hermes =
      frontend "hermes" "NixLoom Hermes agent gateway";
    systemd.user.services.nixloom-webui =
      frontend "webui" "NixLoom Open WebUI";
    systemd.user.services.nixloom-sillytavern =
      frontend "sillytavern" "NixLoom SillyTavern";
  };
}
