{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.nixloom;
  stateDir = toString cfg.stateDir;
  dataDir = toString cfg.dataDir;
  cacheDir = toString cfg.cacheDir;
  configFile = toString cfg.configFile;
  configDir = builtins.dirOf configFile;
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
      "NIXLOOM_STATE_DIR=${stateDir}"
      "NIXLOOM_DATA_DIR=${dataDir}"
      "NIXLOOM_CACHE_DIR=${cacheDir}"
      "NIXLOOM_CONFIG_FILE=${configFile}"
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
      default = "${config.xdg.stateHome}/nixloom";
      defaultText = lib.literalExpression ''"${config.xdg.stateHome}/nixloom"'';
      description = "Absolute writable directory for runtime state, caches, frontend databases and generated keys.";
    };
    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.dataHome}/nixloom";
      defaultText = lib.literalExpression ''"${config.xdg.dataHome}/nixloom"'';
      description = "Absolute writable directory for model weights and model-related data.";
    };
    cacheDir = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.cacheHome}/nixloom";
      defaultText = lib.literalExpression ''"${config.xdg.cacheHome}/nixloom"'';
      description = "Absolute writable directory for re-downloadable caches such as the generated llama-swap config and frontend model caches.";
    };
    configFile = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.configHome}/nixloom/config.yaml";
      defaultText = lib.literalExpression
        ''"${config.xdg.configHome}/nixloom/config.yaml"'';
      description = "User-owned, mutable YAML configuration. The module copies the packaged template here when absent.";
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
        assertion = lib.hasPrefix "/" dataDir;
        message = "services.nixloom.dataDir must be an absolute path";
      }
      {
        assertion = lib.hasPrefix "/" cacheDir;
        message = "services.nixloom.cacheDir must be an absolute path";
      }
      {
        assertion = lib.hasPrefix "/" configFile;
        message = "services.nixloom.configFile must be an absolute path";
      }
      {
        assertion =
          let
            distinct = a: b: a != b && ! lib.hasPrefix "${a}/" b && ! lib.hasPrefix "${b}/" a;
          in
          distinct stateDir dataDir && distinct stateDir cacheDir && distinct dataDir cacheDir;
        message = "services.nixloom.stateDir, dataDir and cacheDir must be separate directories";
      }
      {
        assertion =
          ! (lib.hasPrefix "${stateDir}/" configFile
            || lib.hasPrefix "${dataDir}/" configFile
            || lib.hasPrefix "${cacheDir}/" configFile);
        message = "services.nixloom.configFile must not live inside stateDir, dataDir or cacheDir";
      }
    ];
    home.packages = [ cfg.package ];
    home.sessionVariables = {
      NIXLOOM_STATE_DIR = stateDir;
      NIXLOOM_DATA_DIR = dataDir;
      NIXLOOM_CACHE_DIR = cacheDir;
      NIXLOOM_CONFIG_FILE = configFile;
    };
    home.activation.nixloomDirectories =
      lib.hm.dag.entryAfter [ "writeBoundary" ] ''
        run mkdir -p ${lib.escapeShellArg configDir}
        run chmod 700 ${lib.escapeShellArg configDir}
        run mkdir -p ${lib.escapeShellArg stateDir}
        run chmod 700 ${lib.escapeShellArg stateDir}
        run mkdir -p ${lib.escapeShellArg dataDir}
        run chmod 700 ${lib.escapeShellArg dataDir}
        run mkdir -p ${lib.escapeShellArg cacheDir}
        run chmod 700 ${lib.escapeShellArg cacheDir}
        if [[ ! -e ${lib.escapeShellArg configFile} ]]; then
          run cp ${lib.escapeShellArg (cfg.package + "/share/nixloom/config.yaml")} ${lib.escapeShellArg configFile}
          run chmod 600 ${lib.escapeShellArg configFile}
        fi
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
