# NixLoom-owned Control UI

This directory is the complete OpenClaw Control UI source tree, vendored from the
same OpenClaw revision used by Nixpkgs.  `nix/packages.nix` replaces OpenClaw's
`ui/` directory with this tree and builds it with the exact `node_modules` closure
that ships with the OpenClaw gateway package.

The gateway protocol remains OpenClaw's protocol: the UI and gateway always come
from the same pinned OpenClaw source.  NixLoom changes belong directly in this
tree; there is no runtime DOM/CSS/JavaScript injection layer.

When updating OpenClaw, re-vendor its `ui/` directory, carry the NixLoom commits
forward as ordinary source changes, then run the Nix package build and the
browser smoke test.  This makes upstream conflicts explicit at update time rather
than leaving a hidden compatibility shim in production.
