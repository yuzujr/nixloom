import { createVideoTool } from './dsh_video.mjs';

export const name = 'nixloom-workspace';
export const inject = ['workspaceRegistry', 'systemPrompt', 'tools', 'attachments'];

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
    const video = process.env.NIXLOOM_DSH_VIDEO_WORKFLOWS;
    if (video) {
        const settings = JSON.parse(video);
        if (
            settings.workflows?.['h3-text-to-video'] &&
            settings.workflows?.['h3-image-to-video']
        ) {
            ctx.tools.register(createVideoTool(ctx, settings));
            ctx.systemPrompt.section({
                name: 'nixloom:video-workflow',
                order: ctx.systemPrompt.getSectionOrder('TOOLS_SDK'),
                text: `本机视频工具使用 MiniMax H3，通过 generate_video 调用。
用户要视频时才使用：纯文字生成传 mode="text-to-video"；让图片动起来或基于图片生成传 mode="image-to-video"，并省略 source_attachment_id 以使用最近一条消息中的唯一图片。
指定旧图片时传它的 source_attachment_id；指定工作区文件时传 source_path。一次只处理一张图。
当前设置为 ${settings.size}、${settings.frames} 帧（约 ${Math.round(settings.frames / 24)} 秒）；生成可能需要数分钟，并独占 GPU，完成后会返回可下载的视频文件。
不要把生图/修图请求路由到视频工具。`,
            });
        }
    }
    await ctx.workspaceRegistry.initializeDefault(async () => path);
}
