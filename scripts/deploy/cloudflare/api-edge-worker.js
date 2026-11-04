// The interim API edge (ADR-0025; docs/runbooks/EDGE-CLOUDFLARE.md): a Cloudflare Worker on the
// api.kuinn.in Custom Domain that forwards each request to the Cloud Run service. Cloudflare Free
// cannot rewrite the Host header, and run.app routes by Host, so the Worker makes the request.
// Vars: ORIGIN_URL (the run.app URL) and EDGE_HOST (api.kuinn.in). Secret: ORIGIN_SECRET (the
// EDGE_ORIGIN_SECRET value). Pasted into the dashboard by the owner; no toolchain, no dependency.
export default {
  async fetch(request, env) {
    if (!env.ORIGIN_URL || !env.EDGE_HOST || !env.ORIGIN_SECRET) {
      return new Response(null, { status: 503 }); // fail closed, no detail
    }
    const url = new URL(request.url);
    // workers.dev and preview URLs skip every zone rule: only the edge host is served.
    if (url.hostname !== env.EDGE_HOST) {
      return new Response(null, { status: 404 });
    }
    // Built on the fixed origin, never resolved against it: a path such as //evil.example/x
    // must stay a path on run.app, so the secret never leaves for another host.
    const base = new URL(env.ORIGIN_URL);
    const target = new URL(env.ORIGIN_URL);
    target.pathname = url.pathname;
    target.search = url.search;
    if (target.origin !== base.origin) {
      return new Response(null, { status: 502 });
    }
    const headers = new Headers(request.headers);
    headers.set("x-kuinn-origin-auth", env.ORIGIN_SECRET);
    headers.set("x-kuinn-client-ip", request.headers.get("cf-connecting-ip") ?? "");
    headers.delete("host");
    const hasBody = request.method !== "GET" && request.method !== "HEAD";
    return fetch(target.toString(), {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      redirect: "manual", // the OAuth 302s reach the browser
    });
  },
};
