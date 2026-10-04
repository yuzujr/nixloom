{
    config,
    lib,
    pkgs,
    ...
}:
let
    cfg = config.services.nixloom;
    stateDir = toString cfg.stateDir;
    libraryPath = lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib ];
    tools = [
        pkgs.nodejs_24
        pkgs.pnpm
        pkgs.bashInteractive
        pkgs.coreutils
        pkgs.git
        pkgs.ripgrep
        pkgs.patch
    ]
    ++ lib.optionals cfg.dsh.tailnet.enable [
        pkgs.tailscale
    ];
    environment = [
        "NIXLOOM_STATE_DIR=${stateDir}"
        "NIXLOOM_DATA_DIR=${toString cfg.dataDir}"
        "NIXLOOM_CACHE_DIR=${toString cfg.cacheDir}"
        "NIXLOOM_CONFIG_FILE=${toString cfg.configFile}"
        "NIXLOOM_DSH_BASH=${pkgs.bashInteractive}/bin/bash"
        "NIXLOOM_DSH_LIBRARY_PATH=${libraryPath}"
        "NIXLOOM_DSH_TAILNET=${if cfg.dsh.tailnet.enable then "1" else "0"}"
        "PATH=${lib.makeBinPath tools}:/run/current-system/sw/bin:/etc/profiles/per-user/${config.home.username}/bin"
    ];
in
{
    options.services.nixloom.dsh = {
        enable = lib.mkEnableOption "the npm-managed NixLoom DSH web frontend" // {
            default = true;
        };
        tailnet.enable = lib.mkEnableOption "direct HTTP access from the owner's Tailscale devices (requires a tailscale0 firewall rule)";
    };

    config = lib.mkIf (cfg.enable && cfg.dsh.enable) {
        home = {
            packages = tools;
            sessionVariables = {
                NIXLOOM_DSH_LIBRARY_PATH = libraryPath;
                NIXLOOM_DSH_BASH = "${pkgs.bashInteractive}/bin/bash";
                NIXLOOM_DSH_TAILNET = if cfg.dsh.tailnet.enable then "1" else "0";
            };
            activation.nixloomDshInstall = lib.hm.dag.entryAfter [ "nixloomDirectories" ] ''
                run env ${
                    lib.concatMapStringsSep " " lib.escapeShellArg environment
                } ${cfg.package}/bin/nixloom __service dsh --prepare-only
            '';
        };
        systemd.user.targets.nixloom.Unit.Wants = lib.mkAfter [ "nixloom-dsh.service" ];
        systemd.user.services.nixloom-dsh = {
            Unit = {
                Description = "NixLoom DeepSeek Harness web frontend";
                Wants = [ "nixloom-runtime.service" ];
                After = [ "nixloom-runtime.service" ];
                PartOf = [ "nixloom.target" ];
                X-SwitchMethod = "restart";
            };
            Service = {
                ExecStart = "${cfg.package}/bin/nixloom __service dsh";
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
