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
            text: `Local image tools use ComfyUI on local GPU.
CRITICAL EXECUTION RULE:
- NEVER run shell commands, bash, or file-reading tools (cat, find, ls, grep) to inspect DSH settings, config files, or providers. ComfyUI is already the configured local engine.
- Call generate_image / edit_image directly and immediately on the first turn without prior checks.

Workflow rules:
- For generate_image and generate_images: pass workflow=${JSON.stringify(generate)}. Omit provider/model overrides.
- For edit_image: always pass workflow=${JSON.stringify(edit)} and exactly one reference image (via source_attachment_id or default inline image). Omit other provider/model overrides.

Prompt Standards for local diffusion models:
1. Text-to-Image Generation (generate_image):
- Format: Write a descriptive English natural language paragraph. Do NOT output comma-separated tag soup.
- Subject Layout & Geometry: For manufactured products, electronics, or complex anatomy, explicitly specify spatial arrangements and part topology (e.g. for controllers, specify asymmetric thumbstick layout with left stick above D-pad and right stick below face buttons) to prevent topological hallucinations.
- Text & Typography: Always enclose any rendered text in quotes (e.g. text reading "SHOP NOW"). Specify exact font style, color, case, and placement (e.g. "bold sans-serif white uppercase letters centered on the pill button"). Explicitly state "clean typography, no extra letters, no typos".
- Lighting & Composition: Specify camera perspective, angle, framing, and clean studio lighting.

2. Image Editing (edit_image):
- Preservation Anchor: Explicitly instruct the model to preserve all unmentioned elements: "Keep the subject, background, lighting, color palette, and overall composition strictly unchanged."
- Surgical Modification: Concretely describe only the target delta (e.g. "Modify only the text on the bottom cyan button: replace existing text with exactly 'SHOP NOW' in crisp white bold uppercase letters; leave the button shape, color, and surrounding details untouched.").
- Do not redesign or alter regions outside the user's explicit request.`,
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
