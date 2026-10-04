import assert from 'node:assert/strict';
import { test } from 'node:test';
import { allowsManagedAccess } from '../src/nixloom/dsh_access.mjs';
import { apply } from '../src/nixloom/dsh_workspace.mjs';

test('local and Tailnet admission rejects Host spoofing and other users', () => {
    const saved = { ...process.env };
    try {
        process.env.NIXLOOM_DSH_LOCAL_ACCESS = '1';
        process.env.NIXLOOM_DSH_TAILNET_HOSTS = '["laptop.example.ts.net:3080","laptop:3080","100.64.0.2:3080"]';
        process.env.NIXLOOM_DSH_TAILNET_PEERS = '["100.64.0.3"]';
        const check = (address, host) => allowsManagedAccess({ socket: { remoteAddress: address } }, host);
        assert.equal(check('127.0.0.1', '127.0.0.1:3080'), true);
        assert.equal(check('127.0.0.1', 'laptop:3080'), true);
        assert.equal(check('::1', 'laptop.example.ts.net:3080'), true);
        assert.equal(check('127.0.0.1', 'evil.example:3080'), false);
        assert.equal(check('100.64.0.3', 'laptop.example.ts.net:3080'), true);
        assert.equal(check('100.64.0.3', 'laptop:3080'), true);
        assert.equal(check('::ffff:100.64.0.3', '100.64.0.2:3080'), true);
        assert.equal(check('100.64.0.4', 'laptop.example.ts.net:3080'), false);
        assert.equal(check('100.64.0.3', '127.0.0.1:3080'), false);
        assert.equal(check('192.168.1.50', 'laptop.example.ts.net:3080'), false);
        assert.equal(check('100.64.0.3', 'evil.example:3080'), false);
        assert.equal(check('100.64.0.3', 'laptop.example.ts.net:80'), false);
        delete process.env.NIXLOOM_DSH_LOCAL_ACCESS;
        assert.equal(check('100.64.0.3', 'laptop.example.ts.net:3080'), false);
    } finally { process.env = saved; }
});

test('workspace provider uses the registry first-use policy', async () => {
    const previous = process.env.NIXLOOM_DSH_WORKSPACE;
    try {
        process.env.NIXLOOM_DSH_WORKSPACE = '/managed/workspace';
        let initialized = false;
        let configure;
        await apply({ on(event, listener) { assert.equal(event, 'internal/config'); configure = listener; }, workspaceRegistry: { async initializeDefault(resolve) {
            assert.equal(await resolve(), '/managed/workspace');
            initialized = true;
        } } });
        assert.equal(initialized, true);
        const previousBash = process.env.NIXLOOM_DSH_BASH;
        process.env.NIXLOOM_DSH_BASH = '/nix/bash';
        assert.deepEqual(configure.call({ runtime: { name: 'terminal-bash' } }, {}, () => ({ timeoutMs: 42 })), { timeoutMs: 42, shellPath: '/nix/bash' });
        assert.deepEqual(configure.call({ runtime: { name: 'other' } }, {}, () => ({ untouched: true })), { untouched: true });
        assert.deepEqual(configure.call({ runtime: { name: 'terminal-bash' } }, {}, () => ({ shellDialect: 'pwsh' })), { shellDialect: 'pwsh' });
        if (previousBash === undefined) delete process.env.NIXLOOM_DSH_BASH;
        else process.env.NIXLOOM_DSH_BASH = previousBash;
        delete process.env.NIXLOOM_DSH_WORKSPACE;
        await assert.rejects(apply({}), /not configured/);
    } finally {
        if (previous === undefined) delete process.env.NIXLOOM_DSH_WORKSPACE;
        else process.env.NIXLOOM_DSH_WORKSPACE = previous;
    }
});

test('managed image guidance selects separate generate and edit workflows', async () => {
    const saved = { ...process.env };
    try {
        process.env.NIXLOOM_DSH_WORKSPACE = '/workspace';
        process.env.NIXLOOM_DSH_IMAGE_WORKFLOWS = JSON.stringify({ generate: 'z-image', edit: 'klein-edit' });
        let section;
        await apply({
            on() {},
            workspaceRegistry: { async initializeDefault(resolve) { assert.equal(await resolve(), '/workspace'); } },
            systemPrompt: { getSectionOrder() { return 30; }, section(value) { section = value; } },
        });
        assert.match(section.text, /generate_image.*workflow="z-image"/);
        assert.match(section.text, /edit_image.*workflow="klein-edit"/);
        assert.match(section.text, /exactly one reference image/);
    } finally { process.env = saved; }
});

test('MiniMax H3 tool uploads one reference, runs through the gateway, and returns a file', async () => {
    const savedEnv = { ...process.env };
    const originalFetch = globalThis.fetch;
    const textGraph = {
        '1': { class_type: 'MiniMaxH3ImageToVideo', inputs: { prompt: '{{prompt}}' } },
        '2': { class_type: 'SaveVideo', inputs: { filename_prefix: 'nixloom/h3' } },
    };
    const imageGraph = {
        '1': { class_type: 'MiniMaxH3ImageToVideo', inputs: { prompt: '{{prompt}}', first_frame: ['3', 0] } },
        '2': { class_type: 'SaveVideo', inputs: { filename_prefix: 'nixloom/h3' } },
        '3': { class_type: 'LoadImage', inputs: { image: '{{image}}' } },
    };
    process.env.NIXLOOM_DSH_WORKSPACE = '/workspace';
    process.env.NIXLOOM_DSH_VIDEO_WORKFLOWS = JSON.stringify({
        workflows: { 'h3-text-to-video': textGraph, 'h3-image-to-video': imageGraph },
        frames: 124,
        size: '864x480',
    });
    process.env.NIXLOOM_DSH_COMFYUI_URL = 'http://127.0.0.1:8188';
    const calls = [];
    globalThis.fetch = async (input, options = {}) => {
        const url = new URL(input);
        calls.push({ url, options });
        if (url.pathname.endsWith('/upload/image')) return Response.json({ name: 'reference.png', subfolder: '' });
        if (url.pathname.endsWith('/prompt')) return Response.json({ prompt_id: 'h3-job' });
        if (url.pathname.endsWith('/history/h3-job')) return Response.json({ 'h3-job': { outputs: { '2': { videos: [{ filename: 'h3-output.mp4', subfolder: 'nixloom/h3', type: 'output' }] } } } });
        if (url.pathname.endsWith('/queue')) return Response.json({ queue_running: [], queue_pending: [] });
        if (url.pathname.endsWith('/view')) return new Response(new Uint8Array([1, 2, 3]));
        throw new Error(`Unexpected request ${url}`);
    };
    try {
        let tool;
        let section;
        await apply({
            on() {},
            tools: { register(value) { tool = value; } },
            attachments: {
                imageLimits: { maxImageBytes: 1024 },
                async readImage(ref) { return { data: new Uint8Array([4, 5]), ref: { ...ref, mediaType: 'image/png', name: 'reference.png' } }; },
                async saveFileStream({ data, name }) {
                    const bytes = [];
                    for await (const chunk of data) bytes.push(...chunk);
                    return { attachmentId: 'sha256:video', name, bytes: bytes.length };
                },
            },
            workspaceRegistry: { async initializeDefault(resolve) { assert.equal(await resolve(), '/workspace'); } },
            systemPrompt: { getSectionOrder() { return 30; }, section(value) { section = value; } },
        });
        assert.equal(tool.name, 'generate_video');
        assert.match(section.text, /MiniMax H3/);
        const agent = { session: {
            header: { cwd: '/workspace' },
            deriveMessages() { return [{ source: { kind: 'user' }, content: [{ type: 'image', attachment: { attachmentId: 'sha256:source', mediaType: 'image/png', bytes: 2 } }] }]; },
        } };
        const result = await tool.execute({ prompt: 'A person waves at the camera.', mode: 'image-to-video' }, { signal: new AbortController().signal, agent });
        assert.equal(result.filename, 'h3-output.mp4');
        assert.equal(result.seconds, 5.2);
        assert.deepEqual(tool.output.render({}, result).map(block => block.type), ['text', 'file']);
        const upload = calls.find(call => call.url.pathname.endsWith('/upload/image'));
        assert.ok(upload);
        const submitted = calls.find(call => call.url.pathname.endsWith('/prompt'));
        const payload = JSON.parse(submitted.options.body);
        assert.equal(payload.prompt['1'].inputs.prompt, 'A person waves at the camera.');
        assert.equal(payload.prompt['3'].inputs.image, 'reference.png');
        assert.ok(calls.find(call => call.url.pathname.endsWith('/view')));
        await tool.execute({ prompt: 'A quiet coastal scene at sunset.', mode: 'text-to-video' }, { signal: new AbortController().signal, agent });
        const textSubmission = calls.filter(call => call.url.pathname.endsWith('/prompt')).at(-1);
        const textPayload = JSON.parse(textSubmission.options.body);
        assert.equal(textPayload.prompt['1'].inputs.prompt, 'A quiet coastal scene at sunset.');
        assert.equal(textPayload.prompt['3'], undefined);
        assert.equal(calls.filter(call => call.url.pathname.endsWith('/upload/image')).length, 1);
    } finally {
        globalThis.fetch = originalFetch;
        process.env = savedEnv;
    }
});
