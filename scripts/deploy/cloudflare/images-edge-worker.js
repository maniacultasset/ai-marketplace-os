// The public image edge (ADR-0026; docs/runbooks/DEPLOY-GCP.md "Public image copies"): a Cloudflare
// Worker on the images.kuinn.in Custom Domain that serves one public listing image per request from
// the public bucket. Read-only: GET and HEAD of an exact object name, nothing else. Names are
// `<26 lowercase base32>.<jpg|png>` (app/services/public_images.py new_public_id, image_keys.py
// public_names), so the bucket can never be listed or walked through this host.
// Vars: IMAGE_BUCKET (the public bucket name) and EDGE_HOST (images.kuinn.in). No secrets.
// Pasted into the dashboard by the owner; no toolchain, no dependency.
const NAME = /^\/([a-z2-7]{26})\.(jpg|png)$/;
const TYPES = { jpg: "image/jpeg", png: "image/png" };

function plain(status, extra) {
  return new Response(null, {
    status,
    headers: { "cache-control": "no-store", "x-content-type-options": "nosniff", ...extra },
  });
}

export default {
  async fetch(request, env) {
    if (!env.IMAGE_BUCKET || !env.EDGE_HOST) {
      return plain(503); // fail closed, no detail
    }
    const url = new URL(request.url);
    // workers.dev and preview URLs skip every zone rule: only the edge host is served.
    if (url.hostname !== env.EDGE_HOST) {
      return plain(404);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return plain(405, { allow: "GET, HEAD" });
    }
    const match = NAME.exec(url.pathname);
    if (!match) {
      return plain(404);
    }
    const [, id, ext] = match;
    // A fixed origin and a validated name: the query string is dropped, so the cache key is one
    // URL per image and nothing from the request reaches the bucket path but the name.
    const origin = `https://storage.googleapis.com/${env.IMAGE_BUCKET}/${id}.${ext}`;
    const upstream = await fetch(origin, {
      method: request.method,
      cf: { cacheEverything: true, cacheTtl: 86400 },
    });
    if (upstream.status === 404 || upstream.status === 403) {
      return plain(404);
    }
    if (!upstream.ok) {
      return plain(502);
    }
    const type = (upstream.headers.get("content-type") || "").split(";")[0].trim();
    if (type !== TYPES[ext]) {
      return plain(404); // only the image type the name promises
    }
    const headers = new Headers();
    headers.set("content-type", type);
    for (const name of ["content-length", "etag", "last-modified"]) {
      const value = upstream.headers.get(name);
      if (value) headers.set(name, value);
    }
    headers.set(
      "cache-control",
      upstream.headers.get("cache-control") || "public, max-age=86400",
    );
    headers.set("x-content-type-options", "nosniff");
    headers.set("content-security-policy", "default-src 'none'; sandbox");
    headers.set("cross-origin-resource-policy", "cross-origin");
    headers.set("strict-transport-security", "max-age=63072000");
    headers.set("referrer-policy", "no-referrer");
    return new Response(request.method === "HEAD" ? null : upstream.body, {
      status: 200,
      headers,
    });
  },
};
