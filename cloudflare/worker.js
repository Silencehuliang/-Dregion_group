// karing-sub Worker —— 只读 Cloudflare KV, 不跑重活(免费版 10ms CPU 也够用)。
//
//   订阅地址:  https://<你的域名>/sub        (?b64=1 返回 base64)
//   统计信息:  https://<你的域名>/meta
//   健康检查:  https://<你的域名>/healthz
//
// KV 绑定: env.SUB_KV   (见 wrangler.toml)
// KV 键:   env.SUB_KEY  (默认 karing_region.yaml)
//          env.META_KEY (默认 karing_region_meta.json)

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "*",
};

function toBase64(buf) {
  const arr = new Uint8Array(buf);
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < arr.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, arr.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

function helpText(origin) {
  return [
    "karing-sub —— 自动更新的 Clash.Meta 订阅",
    "",
    `  订阅:     ${origin}/sub`,
    `  统计:     ${origin}/meta`,
    `  健康检查: ${origin}/healthz`,
    "",
    "在 Karing / mihomo / Clash.Meta 里添加订阅即可。",
  ].join("\n");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    let path = url.pathname.replace(/\/+$/, "");
    if (path === "") path = "/";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    if (path === "/healthz") {
      return new Response("ok", {
        headers: { ...CORS, "Content-Type": "text/plain; charset=utf-8" },
      });
    }

    if (path === "/") {
      return new Response(helpText(url.origin), {
        headers: { ...CORS, "Content-Type": "text/plain; charset=utf-8" },
      });
    }

    if (path === "/meta") {
      const metaKey = env.META_KEY || "karing_region_meta.json";
      const v = await env.SUB_KV.get(metaKey, { cacheTtl: 60 });
      if (!v) {
        return new Response('{"error":"not ready"}', {
          status: 404,
          headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" },
        });
      }
      return new Response(v, {
        headers: {
          ...CORS,
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "public, max-age=60",
        },
      });
    }

    if (path === "/sub") {
      const key = env.SUB_KEY || "karing_region.yaml";
      const buf = await env.SUB_KV.get(key, { type: "arrayBuffer", cacheTtl: 60 });
      if (!buf) {
        return new Response("subscription not ready", {
          status: 404,
          headers: { ...CORS, "Content-Type": "text/plain; charset=utf-8" },
        });
      }
      const common = { ...CORS, "Cache-Control": "public, max-age=60" };
      if (url.searchParams.get("b64") === "1") {
        return new Response(toBase64(buf), {
          headers: { ...common, "Content-Type": "text/plain; charset=utf-8" },
        });
      }
      return new Response(buf, {
        headers: {
          ...common,
          "Content-Type": "text/yaml; charset=utf-8",
          "Content-Disposition": 'inline; filename="karing_region.yaml"',
        },
      });
    }

    return new Response("not found", {
      status: 404,
      headers: { ...CORS, "Content-Type": "text/plain; charset=utf-8" },
    });
  },
};
