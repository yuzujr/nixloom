{ self, nixpkgs }:
let
  core = import ./core.nix { inherit self nixpkgs; };
  openclaw = import ./openclaw.nix { inherit self; };
  sillytavern = import ./sillytavern.nix { inherit self; };
in
{
  inherit core;
  openclaw.imports = [
    core
    openclaw
  ];
  sillytavern.imports = [
    core
    sillytavern
  ];
  default.imports = [
    core
    openclaw
    sillytavern
  ];
}
