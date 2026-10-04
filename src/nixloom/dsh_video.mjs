import { readFile, realpath } from 'node:fs/promises';
import { extname, isAbsolute, relative, resolve, sep } from 'node:path';

const videoToolOutput = {
    schema: {
        type: 'object',
        additionalProperties: false,
        required: ['attachment', 'filename', 'mode', 'size', 'seconds'],
        properties: {
            attachment: {
                type: 'object',
                additionalProperties: false,
                required: ['attachmentId', 'name', 'bytes'],
                properties: {
                    attachmentId: { type: 'string' },
                    name: { type: 'string' },
                    bytes: { type: 'integer' },
                },
            },
            filename: { type: 'string' },
            mode: { type: 'string', enum: ['text-to-video', 'image-to-video'] },
            size: { type: 'string' },
            seconds: { type: 'number' },
        },
    },
    render(_args, value) {
        return [
            { type: 'text', text: `MiniMax H3 视频已生成：${value.filename}（${value.size}，约 ${value.seconds} 秒）。` },
            { type: 'file', attachment: value.attachment },
        ];
    },
};

export function createVideoTool(ctx, settings) {
    return {
        name: 'generate_video',
        description: '用本机 MiniMax H3 从文字生成带音频的视频，或让一张参考图动起来。H3 生成较慢，会独占 GPU；图生视频可使用最新消息中的图片，也可指定旧附件或工作区图片。',
        parameters: {
            type: 'object',
            additionalProperties: false,
            required: ['prompt', 'mode'],
            properties: {
                prompt: { type: 'string', minLength: 1, description: '视频内容、镜头运动、动作和环境声音描述。' },
                mode: { type: 'string', enum: ['text-to-video', 'image-to-video'], description: '纯文字生成，或根据一张图片生成视频。' },
                source_attachment_id: { type: 'string', description: '可选：指定对话中较早的一张图片；省略时从最近相关消息取唯一图片。' },
                source_path: { type: 'string', description: '可选：工作区内图片的相对路径或绝对路径；不能与 source_attachment_id 同时使用。' },
            },
        },
        timeoutMs: 60 * 60 * 1000,
        output: videoToolOutput,
        async execute(args, exec) {
            const imageMode = args.mode === 'image-to-video';
            if (args.mode !== 'text-to-video' && !imageMode) throw new Error('mode 必须是 text-to-video 或 image-to-video');
            if (!imageMode && (args.source_attachment_id || args.source_path)) {
                throw new Error('纯文字生成不能带 source_attachment_id 或 source_path');
            }
            if (args.source_attachment_id && args.source_path) {
                throw new Error('source_attachment_id 和 source_path 只能指定一个');
            }
            const workflow = structuredClone(settings.workflows[imageMode ? 'h3-image-to-video' : 'h3-text-to-video']);
            const imageName = imageMode ? await uploadReference(ctx, args, exec) : undefined;
            const promptId = await submitVideo(workflow, args.prompt, imageName, exec.signal);
            const completed = await waitForVideo(promptId, exec.signal);
            const output = firstVideo(completed);
            const attachment = await downloadVideo(ctx, output, exec.signal);
            return {
                attachment,
                filename: attachment.name,
                mode: args.mode,
                size: settings.size,
                seconds: Math.round(settings.frames / 24 * 10) / 10,
            };
        },
    };
}

async function uploadReference(ctx, args, exec) {
    let image;
    if (args.source_path) image = await readWorkspaceImage(ctx, args.source_path, exec);
    else {
        const reference = findImageReference(exec.agent, args.source_attachment_id);
        if (!reference) throw new Error('图生视频需要一张图片；请上传图片，或指定 source_attachment_id/source_path。');
        const stored = await ctx.attachments.readImage(reference, exec.signal);
        image = { data: stored.data, mediaType: stored.ref.mediaType, name: stored.ref.name };
    }
    const extension = ({ 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp' })[image.mediaType];
    if (!extension) throw new Error(`H3 图生视频只支持 PNG、JPEG 或 WebP；收到 ${image.mediaType}`);
    const form = new FormData();
    form.append('image', new Blob([image.data], { type: image.mediaType }), `nixloom-h3-${crypto.randomUUID()}.${extension}`);
    const response = await fetch(comfyUrl('upload/image'), { method: 'POST', body: form, signal: exec.signal, redirect: 'error' });
    const result = await jsonResponse(response, 'ComfyUI 图片上传失败');
    if (typeof result.name !== 'string' || result.name.length === 0) throw new Error('ComfyUI 图片上传没有返回文件名');
    return typeof result.subfolder === 'string' && result.subfolder ? `${result.subfolder}/${result.name}` : result.name;
}

async function readWorkspaceImage(ctx, sourcePath, exec) {
    const rootValue = exec.agent?.session?.header?.cwd;
    if (!rootValue) throw new Error('source_path 需要一个活动的 DSH 工作区');
    const root = resolve(rootValue);
    const target = isAbsolute(sourcePath) ? resolve(sourcePath) : resolve(root, sourcePath);
    if (!inside(root, target)) throw new Error('source_path 必须位于当前 DSH 工作区内');
    const [realRoot, realTarget] = await Promise.all([realpath(root), realpath(target)]);
    if (!inside(realRoot, realTarget)) throw new Error('source_path 不能通过符号链接逃出当前 DSH 工作区');
    const mediaType = ({ '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp' })[extname(target).toLowerCase()];
    if (!mediaType) throw new Error('source_path 必须指向 PNG、JPEG 或 WebP 图片');
    const data = new Uint8Array(await readFile(realTarget, { signal: exec.signal }));
    if (data.byteLength > ctx.attachments.imageLimits.maxImageBytes) throw new Error('参考图片超过 DSH 的上传大小限制');
    await ctx.attachments.validateImage({ data, mediaType, name: target.split(sep).at(-1) });
    return { data, mediaType };
}

function findImageReference(agent, requestedId) {
    const messages = agent?.session?.deriveMessages?.() ?? [];
    function imageBlocks(blocks) {
        const images = [];
        for (const block of blocks) {
            if (block.type === 'image' && block.attachment) images.push(block.attachment);
            if (Array.isArray(block.content)) images.push(...imageBlocks(block.content));
        }
        return images;
    }
    const matches = requestedId
        ? messages.flatMap(message => imageBlocks(message.content ?? [])).filter(ref => idsMatch(String(ref.attachmentId), requestedId))
        : (() => {
            const latestUser = [...messages].reverse().find(message => message.source?.kind === 'user');
            const latestImages = latestUser ? imageBlocks(latestUser.content ?? []) : [];
            if (latestImages.length) return latestImages;
            for (let index = messages.length - 1; index >= 0; index -= 1) {
                const refs = imageBlocks(messages[index]?.content ?? []);
                if (refs.length) return refs;
            }
            return [];
        })();
    if (matches.length > 1) throw new Error('最近消息有多张图片；请指定要使用的 source_attachment_id。');
    return matches[0];
}

function idsMatch(actual, requested) {
    if (actual === requested) return true;
    const digest = value => /^(?:sha256:)?([0-9a-f]{64})$/i.exec(value.trim())?.[1]?.toLowerCase();
    return digest(actual) !== undefined && digest(actual) === digest(requested);
}

function inside(parent, child) {
    const path = relative(parent, child);
    return path === '' || (path !== '..' && !path.startsWith(`..${sep}`) && !isAbsolute(path));
}

function comfyUrl(path) {
    const base = process.env.NIXLOOM_DSH_COMFYUI_URL;
    if (!base) throw new Error('DSH 的 H3 视频地址未配置；请重新激活 NixLoom');
    return `${base.replace(/\/$/, '')}/${path}`;
}

async function submitVideo(workflow, prompt, imageName, signal) {
    let replacedPrompt = false;
    let replacedImage = false;
    const seed = Math.floor(Math.random() * 2 ** 32);
    for (const node of Object.values(workflow)) {
        for (const [key, value] of Object.entries(node.inputs ?? {})) {
            if (typeof value !== 'string') continue;
            if (value.includes('{{prompt}}')) {
                node.inputs[key] = value.replaceAll('{{prompt}}', prompt);
                replacedPrompt = true;
            } else if (value === '{{seed}}') node.inputs[key] = seed;
            else if (value === '{{image}}') {
                if (!imageName) throw new Error('该 H3 工作流需要一张参考图');
                node.inputs[key] = imageName;
                replacedImage = true;
            }
        }
    }
    if (!replacedPrompt) throw new Error('H3 工作流缺少 {{prompt}} 占位符');
    if (imageName && !replacedImage) throw new Error('H3 图生视频工作流缺少 {{image}} 占位符');
    const response = await fetch(comfyUrl('prompt'), {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify({ prompt: workflow }),
        signal,
        redirect: 'error',
    });
    const result = await jsonResponse(response, 'ComfyUI 没有接受 H3 视频任务');
    if (typeof result.prompt_id !== 'string' || !result.prompt_id) {
        throw new Error(`ComfyUI 未返回任务编号：${JSON.stringify(result).slice(0, 1500)}`);
    }
    return result.prompt_id;
}

async function waitForVideo(promptId, signal) {
    while (true) {
        signal.throwIfAborted();
        const [history, queue] = await Promise.all([
            jsonGet(comfyUrl(`history/${encodeURIComponent(promptId)}`), signal),
            jsonGet(comfyUrl('queue'), signal),
        ]);
        const entry = history[promptId];
        if (entry && queue.queue_running.length === 0 && queue.queue_pending.length === 0) {
            if (entry.status?.status_str === 'error') throw new Error('ComfyUI 的 H3 视频任务失败；查看 nixloom logs runtime。');
            return entry;
        }
        if (queue.queue_running.length === 0 && queue.queue_pending.length === 0 && !entry) {
            throw new Error('H3 任务已离开 ComfyUI 队列，但没有生成结果；查看 nixloom logs runtime。');
        }
        await delay(2000, signal);
    }
}

function firstVideo(entry) {
    for (const output of Object.values(entry.outputs ?? {})) {
        for (const key of ['videos', 'gifs', 'images']) {
            for (const video of output[key] ?? []) {
                if (typeof video.filename === 'string' && /\.(mp4|webm|mov|mkv|gif)$/i.test(video.filename)) return video;
            }
        }
    }
    throw new Error('H3 任务完成，但 ComfyUI 历史记录里没有视频文件。');
}

async function downloadVideo(ctx, output, signal) {
    const params = new URLSearchParams({ filename: output.filename, subfolder: output.subfolder ?? '', type: output.type ?? 'output' });
    const response = await fetch(comfyUrl(`view?${params}`), { signal, redirect: 'error' });
    if (!response.ok || !response.body) throw new Error(`无法从 ComfyUI 取回视频（HTTP ${response.status}）`);
    const filename = output.filename.split('/').at(-1);
    return ctx.attachments.saveFileStream({ data: response.body, signal, name: filename });
}

async function jsonGet(url, signal) {
    const response = await fetch(url, { signal, redirect: 'error' });
    return jsonResponse(response, `ComfyUI 请求失败 (${new URL(url).pathname})`);
}

async function jsonResponse(response, label) {
    const text = await response.text();
    let value;
    try { value = JSON.parse(text); } catch { value = undefined; }
    if (!response.ok) throw new Error(`${label}（HTTP ${response.status}）：${text.slice(0, 1500)}`);
    if (!value || typeof value !== 'object') throw new Error(`${label}：ComfyUI 返回了无效 JSON`);
    return value;
}

function delay(milliseconds, signal) {
    return new Promise((resolveDelay, reject) => {
        const timer = setTimeout(done, milliseconds);
        function done() { signal.removeEventListener('abort', abort); resolveDelay(); }
        function abort() { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(signal.reason); }
        signal.addEventListener('abort', abort, { once: true });
        if (signal.aborted) abort();
    });
}
