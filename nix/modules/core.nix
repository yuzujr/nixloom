{ self, nixpkgs }:
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
  command = "${cfg.package}/bin/nixloom";
  pinned = self.packages.${pkgs.stdenv.hostPlatform.system};
  capabilityPkgs = import nixpkgs {
    system = pkgs.stdenv.hostPlatform.system;
    config = {
      allowUnfree = true;
      cudaCapabilities = cfg.cudaCapabilities;
    };
  };
  llamaPackages = {
    cpu = pinned.llama-cpu;
    cuda =
      if cfg.cudaCapabilities == [ ] then
        pinned.llama-cuda
      else
        capabilityPkgs.llama-cpp.override {
          cudaSupport = true;
          vulkanSupport = false;
        };
    vulkan = pinned.llama-vulkan;
    rocm = pinned.llama-rocm;
  };
  imagePackages = {
    cpu = pinned.image-cpu;
    cuda =
      if cfg.cudaCapabilities == [ ] then pinned.image-cuda else capabilityPkgs.stable-diffusion-cpp-cuda;
    vulkan = pinned.image-vulkan;
    rocm = pinned.image-rocm;
  };
  environment = [
    "NIXLOOM_STATE_DIR=${stateDir}"
    "NIXLOOM_DATA_DIR=${dataDir}"
    "NIXLOOM_CACHE_DIR=${cacheDir}"
    "NIXLOOM_CONFIG_FILE=${configFile}"
    "NIXLOOM_ACCELERATION=${cfg.acceleration}"
    "NIXLOOM_IMAGE_RUNTIME=${if cfg.images.enable then "enabled" else "disabled"}"
    "PATH=${
      lib.makeBinPath (
        [
          cfg.package
          cfg.swapPackage
          cfg.llamaPackage
        ]
        ++ lib.optional cfg.images.enable cfg.imagePackage
      )
    }"
  ]
  ++ lib.optional (cfg.acceleration == "cuda") "LD_LIBRARY_PATH=/run/opengl-driver/lib";
in
{
  options.services.nixloom = {
    enable = lib.mkEnableOption "the NixLoom local AI runtime";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "NixLoom Python control package.";
    };
    acceleration = lib.mkOption {
      type = lib.types.enum [
        "cpu"
        "cuda"
        "vulkan"
        "rocm"
      ];
      default = "cpu";
      description = "Hardware backend selected independently of the YAML runtime configuration.";
    };
    cudaCapabilities = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "12.0" ];
      description = ''
        CUDA compute capabilities to compile into CUDA backends. An empty list
        uses the pinned nixpkgs defaults; declaring the host capabilities avoids
        compiling kernels for unrelated GPU architectures.
      '';
    };
    llamaPackage = lib.mkOption {
      type = lib.types.package;
      default = llamaPackages.${cfg.acceleration};
      description = "llama.cpp package; override this for a custom backend or build.";
    };
    imagePackage = lib.mkOption {
      type = lib.types.package;
      default = imagePackages.${cfg.acceleration};
      description = "stable-diffusion.cpp package; override this for a custom backend or build.";
    };
    swapPackage = lib.mkOption {
      type = lib.types.package;
      default = pinned.llama-swap;
      description = "Pinned llama-swap package.";
    };
    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.stateHome}/nixloom";
      description = "Writable runtime and frontend state directory.";
    };
    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.dataHome}/nixloom";
      description = "Model data directory.";
    };
    cacheDir = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.cacheHome}/nixloom";
      description = "Reproducible runtime cache directory.";
    };
    configFile = lib.mkOption {
      type = lib.types.str;
      default = "${config.xdg.configHome}/nixloom/config.yaml";
      description = "User-owned YAML configuration.";
    };
    autoStart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Start the runtime target at login.";
    };
    images.enable = lib.mkEnableOption "the stable-diffusion.cpp image runtime";
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.all (path: lib.hasPrefix "/" path) [
          stateDir
          dataDir
          cacheDir
          configFile
        ];
        message = "services.nixloom writable paths and configFile must be absolute";
      }
      {
        assertion =
          lib.length (
            lib.unique [
              stateDir
              dataDir
              cacheDir
            ]
          ) == 3;
        message = "services.nixloom stateDir, dataDir and cacheDir must be distinct";
      }
      {
        assertion = lib.all (
          capability: builtins.match "^[0-9]+\\.[0-9]+$" capability != null
        ) cfg.cudaCapabilities;
        message = "services.nixloom.cudaCapabilities entries must look like \"12.0\"";
      }
      {
        assertion = cfg.acceleration != "rocm" || pkgs.stdenv.hostPlatform.isx86_64;
        message = "services.nixloom.acceleration = \"rocm\" is currently supported only on x86_64-linux";
      }
    ];

    home.packages = [ cfg.package ];
    home.sessionVariables = {
      NIXLOOM_STATE_DIR = stateDir;
      NIXLOOM_DATA_DIR = dataDir;
      NIXLOOM_CACHE_DIR = cacheDir;
      NIXLOOM_CONFIG_FILE = configFile;
      NIXLOOM_ACCELERATION = cfg.acceleration;
      NIXLOOM_IMAGE_RUNTIME = if cfg.images.enable then "enabled" else "disabled";
    };

    home.activation.nixloomDirectories = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      run mkdir -p ${lib.escapeShellArg (builtins.dirOf configFile)}
      run chmod 700 ${lib.escapeShellArg (builtins.dirOf configFile)}
      run mkdir -p ${lib.escapeShellArg stateDir} ${lib.escapeShellArg dataDir} ${lib.escapeShellArg cacheDir}
      run chmod 700 ${lib.escapeShellArg stateDir} ${lib.escapeShellArg dataDir} ${lib.escapeShellArg cacheDir}
      if [[ ! -e ${lib.escapeShellArg configFile} ]]; then
        run cp ${
          lib.escapeShellArg (cfg.package + "/share/nixloom/config.yaml")
        } ${lib.escapeShellArg configFile}
        run chmod 600 ${lib.escapeShellArg configFile}
      fi
    '';

    systemd.user.targets.nixloom = {
      Unit = {
        Description = "NixLoom local AI runtime";
        Wants = [ "nixloom-runtime.service" ];
        After = [ "nixloom-runtime.service" ];
      };
      Install.WantedBy = lib.optional cfg.autoStart "default.target";
    };

    systemd.user.services.nixloom-runtime = {
      Unit = {
        Description = "NixLoom model swap runtime";
        PartOf = [ "nixloom.target" ];
        X-SwitchMethod = "keep-old";
      };
      Service = {
        ExecStart = "${command} __service runtime";
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
