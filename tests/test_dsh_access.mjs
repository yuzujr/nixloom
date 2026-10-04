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
