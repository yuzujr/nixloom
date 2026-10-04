{ self, nixpkgs }:
let
    core = import ./core.nix { inherit self nixpkgs; };
    dsh = import ./dsh.nix;
    sillytavern = import ./sillytavern.nix { inherit self; };
in
{
    inherit core;
    dsh.imports = [
        core
        dsh
    ];
    sillytavern.imports = [
        core
        sillytavern
    ];
    default.imports = [
        core
        dsh
        sillytavern
    ];
}
