// Nix-built Node does not match the native addon's private getter patterns.
// The launcher exposes these same internals through Node's own module loader.
const { createRequire } = require("node:module");
const appRequire = createRequire(process.argv[1]);
appRequire("node-addon-require-builtin").requireBuiltin = (id) => appRequire(id);

if (process.env.NIXLOOM_DSH_LOCAL_PROXY_TARGET) {
  const net = require("node:net");
  const target = process.env.NIXLOOM_DSH_LOCAL_PROXY_TARGET;
  const port = Number(process.env.NIXLOOM_DSH_LOCAL_PROXY_PORT);
  const proxy = net.createServer((client) => {
    const upstream = net.createConnection({ host: target, port });
    client.on("error", () => upstream.destroy());
    upstream.on("error", () => client.destroy());
    upstream.once("connect", () => {
      client.pipe(upstream);
      upstream.pipe(client);
    });
  });
  proxy.on("error", (error) => {
    console.error(`DSH localhost proxy failed: ${error.message}`);
    process.exit(1);
  });
  proxy.listen(port, "127.0.0.1");
}
