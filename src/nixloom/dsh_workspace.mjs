export const name = 'nixloom-workspace';
export const inject = ['workspaceRegistry', 'systemPrompt'];

export async function apply(ctx) {
    const path = process.env.NIXLOOM_DSH_WORKSPACE;
    if (!path) throw new Error('NixLoom workspace is not configured.');
    // Presets create terminal fibers lazily; configure each through Cordis.
    ctx.on("internal/config", function (_raw, next) {
        const config = next();
        if (this.runtime?.name !== "terminal-bash" || config?.shellDialect === "pwsh") return config;
        return { ...config, shellPath: process.env.NIXLOOM_DSH_BASH || config?.shellPath };
    });
    const workflows = process.env.NIXLOOM_DSH_IMAGE_WORKFLOWS;
    if (workflows) {
        const { generate, edit } = JSON.parse(workflows);
        ctx.systemPrompt.section({
            name: 'nixloom:image-workflows',
            order: ctx.systemPrompt.getSectionOrder('TOOLS_SDK'),
            text: `Local image tools use ComfyUI. For generate_image and generate_images, pass workflow=${JSON.stringify(generate)}. For edit_image, always pass workflow=${JSON.stringify(edit)} and exactly one reference image. Omit other provider overrides. For editing, describe the requested change and explicitly preserve the reference person's identity and all unchanged details.`,
        });
    }
    await ctx.workspaceRegistry.initializeDefault(async () => path);
}
