import { createVideoTool } from './dsh_video.mjs';

export const name = 'nixloom-workspace';
export const inject = ['workspaceRegistry', 'systemPrompt', 'tools', 'attachments'];

export async function apply(ctx) {
    const path = process.env.NIXLOOM_DSH_WORKSPACE;
    if (!path) throw new Error('NixLoom workspace is not configured.');
    // Presets create terminal fibers lazily; configure each through Cordis.
    ctx.on("internal/config", function (fiber, next) {
        const config = next();
        const runtimeName = fiber?.runtime?.name || this?.runtime?.name;
        if (runtimeName !== "terminal-bash" || config?.shellDialect === "pwsh") return config;
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
- 仅在用户明确要求生成视频或让图片动起来时使用。纯文字生成传 mode="text-to-video"；基于图片生成传 mode="image-to-video"（默认省略 source_attachment_id 自动使用最近一张图片）。
- 指定较早对话图片传 source_attachment_id，工作区文件传 source_path。一次只处理一张图。
- 当前设置为 ${settings.size}、${settings.frames} 帧（约 ${Math.round(settings.frames / 24)} 秒），生成需独占 GPU 数分钟。
- 严禁将普通的生图或修图请求路由至 generate_video。

Prompt Standards for MiniMax H3 Video Generation:
- Language: ALWAYS translate and expand the user's intent into professional English cinematic prose.
- Motion & Camera Trajectory: Always explicitly define camera movement (e.g. "smooth 360-degree orbital pan", "slow cinematic push-in / dolly forward", "fluid tracking shot", "steady handheld camera with subtle organic drift") to prevent static outputs.
- Temporal Dynamics: Describe motion progression across time: subject action, speed, and environmental dynamics (e.g. moving lighting reflections, particle flow, cloth flutter).
- Mode Rules:
  * mode="text-to-video": Specify [Subject & Staging] + [Subject Action] + [Camera Trajectory] + [Lighting & 24fps Commercial Style].
  * mode="image-to-video": Focus 90% on MOTION and CAMERA PATH. Do NOT redesign or extensively re-describe the reference image's appearance; keep the subject and environment consistent while executing the specified motion and camera trajectory.`,
            });
        }
    }
    await ctx.workspaceRegistry.initializeDefault(async () => path);
}
