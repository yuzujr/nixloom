// Nix-built Node does not match the native addon's private getter patterns.
// The launcher exposes these same internals through Node's own module loader.
const { createRequire } = require("node:module");
const appRequire = createRequire(process.argv[1]);
appRequire("node-addon-require-builtin").requireBuiltin = (id) => appRequire(id);
