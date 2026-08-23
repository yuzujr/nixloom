{ pkgs }:
let
  python = pkgs.python3.withPackages (packages: [ packages.pyyaml ]);
in
pkgs.mkShell {
  packages = [
    python
    pkgs.nixfmt
    pkgs.ruff
  ];
  shellHook = ''
    export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
  '';
}
