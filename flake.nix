{
  description = "NixLoom: a modular local-AI runtime for NixOS";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.home-manager = {
    url = "github:nix-community/home-manager";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = lib.genAttrs systems;
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config = {
            allowUnfree = true;
            allowInsecurePredicate = pkg: lib.getName pkg == "openclaw";
          };
        };
      source = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./pyproject.toml
          ./config.yaml
          ./src
          ./tests
        ];
      };
      architectureSource = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./README.md
          ./config.yaml
          ./flake.nix
          ./nix
          ./pyproject.toml
          ./src
          ./tests
        ];
      };
    in
    {
      packages = forAllSystems (
        system:
        import ./nix/packages.nix {
          inherit lib source;
          pkgs = pkgsFor system;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/nixloom";
        };
      });

      checks = forAllSystems (
        system:
        import ./nix/checks.nix {
          inherit
            self
            lib
            source
            architectureSource
            ;
          inherit home-manager;
          pkgs = pkgsFor system;
        }
      );

      devShells = forAllSystems (system: {
        default = import ./nix/dev-shell.nix { pkgs = pkgsFor system; };
      });

      homeManagerModules = import ./nix/modules { inherit self nixpkgs; };
    };
}
