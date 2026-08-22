export async function onRequestGet(context) {
  const assetUrl = new URL(context.request.url);
  assetUrl.pathname = "/health.json";
  assetUrl.search = "";
  const asset = await context.env.ASSETS.fetch(assetUrl);
  const headers = new Headers(asset.headers);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("Cache-Control", "no-cache");
  headers.set("X-Content-Type-Options", "nosniff");
  return new Response(asset.body, { status: asset.status, headers });
}

export function onRequestOptions() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Max-Age": "86400",
    },
  });
}

