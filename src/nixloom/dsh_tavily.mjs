export const name = "nixloom-tavily";
export const inject = ["web"];

export function apply(ctx) {
    ctx.web.registerSearchProvider({
        id: "tavily",
        available: () => Boolean(process.env.TAVILY_API_KEY),
        async search(request, signal) {
            const key = process.env.TAVILY_API_KEY;
            if (!key) throw new Error("Tavily API key is missing from NixLoom credentials.");
            const response = await fetch("https://api.tavily.com/search", {
                method: "POST",
                redirect: "error",
                headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
                body: JSON.stringify({ query: request.query, max_results: Math.min(request.maxResults ?? 5, 20), search_depth: "basic" }),
                signal,
            });
            if (!response.ok) throw new Error(`Tavily search failed (HTTP ${response.status}).`);
            const result = await response.json();
            if (!Array.isArray(result.results)) throw new Error("Tavily returned an invalid search response.");
            return {
                sources: result.results.map(item => ({ url: item.url, title: item.title, snippet: item.content })),
                truncated: false,
            };
        },
    });
}
