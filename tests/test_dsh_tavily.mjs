import assert from 'node:assert/strict';
import { test } from 'node:test';
import { apply } from '../src/nixloom/dsh_tavily.mjs';

test('Tavily request, result mapping and failure handling', async () => {
    let provider;
    apply({ web: { registerSearchProvider(value) { provider = value; } } });
    const previousKey = process.env.TAVILY_API_KEY;
    const previousFetch = globalThis.fetch;
    try {
        delete process.env.TAVILY_API_KEY;
        assert.equal(provider.available(), false);
        await assert.rejects(provider.search({ query: 'test' }), /key is missing/);
        process.env.TAVILY_API_KEY = 'test-secret';
        const signal = new AbortController().signal;
        globalThis.fetch = async (url, options) => {
            assert.equal(url, 'https://api.tavily.com/search');
            assert.equal(options.headers.Authorization, 'Bearer test-secret');
            assert.equal(options.signal, signal);
            assert.equal(options.redirect, 'error');
            assert.deepEqual(JSON.parse(options.body), { query: 'test', max_results: 20, search_depth: 'basic' });
            return { ok: true, json: async () => ({ results: [{ url: 'https://example.com', title: 'Example', content: 'Result' }] }) };
        };
        assert.equal(provider.available(), true);
        assert.deepEqual(await provider.search({ query: 'test', maxResults: 99 }, signal), {
            sources: [{ url: 'https://example.com', title: 'Example', snippet: 'Result' }], truncated: false,
        });
        globalThis.fetch = async () => ({ ok: false, status: 401 });
        await assert.rejects(provider.search({ query: 'test' }), { message: 'Tavily search failed (HTTP 401).' });
        globalThis.fetch = async () => ({ ok: true, json: async () => ({}) });
        await assert.rejects(provider.search({ query: 'test' }), /invalid search response/);
    } finally {
        globalThis.fetch = previousFetch;
        if (previousKey === undefined) delete process.env.TAVILY_API_KEY;
        else process.env.TAVILY_API_KEY = previousKey;
    }
});
