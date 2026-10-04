export const name = 'nixloom-workspace';
export const inject = ['workspaceRegistry'];

export async function apply(ctx) {
    const path = process.env.NIXLOOM_DSH_WORKSPACE;
    if (!path) throw new Error('NixLoom workspace is not configured.');
    // Presets create terminal fibers lazily; configure each through Cordis.
    ctx.on("internal/config", function (_raw, next) {
        const config = next();
        if (this.runtime?.name !== "terminal-bash" || config?.shellDialect === "pwsh") return config;
        return { ...config, shellPath: process.env.NIXLOOM_DSH_BASH || config?.shellPath };
    });
    await ctx.workspaceRegistry.initializeDefault(async () => path);
}
