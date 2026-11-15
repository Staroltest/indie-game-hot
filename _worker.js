// IndieHOT MCP endpoint — Cloudflare Pages Advanced Mode.
//
// Single self-contained file (no imports): it is uploaded as the deployment's
// `_worker.js` form field by cf.py, NOT as a static asset. `_routes.json`
// routes only /mcp to this worker; every other path is served statically.
//
// Protocol: MCP Streamable HTTP, stateless (no session ids, no SSE streams).
// POST /mcp accepts JSON-RPC 2.0: initialize, ping, tools/list, tools/call.
// Data comes from the site's own public JSON on the GitHub Pages origin, so
// the worker needs no bindings and stays in sync with every half-hourly push.
//
// Copyright rule: only titles + self-written one-line descriptions + outbound
// links are ever returned. The public JSON files carry nothing else.
//
// Bilingual (2026-11-15): every tool accepts an optional `lang` argument
// ("zh", default, or "en"). lang=en reads the /en/data/ tree, whose items
// carry en_title/en_oneliner written from our own Chinese one-liners —
// never source summaries. The Chinese tree carries no en_* fields, so the
// two languages must read different paths; shapeItem picks fields by lang.

const DATA_ORIGIN = "https://staroltest.github.io/indie-game-hot";
const SERVER_INFO = { name: "indiehot", version: "1.1.0" };
const KNOWN_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const CATEGORIES = ["engine", "publishing", "devlog", "industry", "events", "reviews", "official"];

const TOOLS = [
  {
    name: "get_hot",
    description: "IndieHOT 当前热点榜：独立游戏开发者社区正在关注的热门动态，按热度排序。返回标题、一句简介、原文链接、来源与热度分。默认中文，lang=en 返回英文版。",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", description: "返回条数，1-20，默认 10。", minimum: 1, maximum: 20, default: 10 },
        lang: { type: "string", description: "返回语言：zh 中文（默认）或 en 英文。", enum: ["zh", "en"], default: "zh" },
      },
    },
  },
  {
    name: "get_daily",
    description: "IndieHOT 最新一期日报：过去 24 小时的收录总数、精选条数与精选内容（含导读摘要）。每天北京时间 06:00 出新一期。默认中文，lang=en 返回英文版。",
    inputSchema: {
      type: "object",
      properties: {
        lang: { type: "string", description: "返回语言：zh 中文（默认）或 en 英文。", enum: ["zh", "en"], default: "zh" },
      },
    },
  },
  {
    name: "search_items",
    description: "搜索 IndieHOT 最近一段时间收录的独立游戏开发者向动态。可按关键词和分类筛选，结果按评分排序。返回标题、一句简介、原文链接、来源、发布时间与分类。默认中文，lang=en 返回英文版，关键词匹配对应语言的标题与简介。",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "关键词，匹配对应语言的标题、原标题、简介与来源（不区分大小写）。留空则不按关键词过滤。" },
        category: {
          type: "string",
          description: "分类筛选。",
          enum: CATEGORIES,
        },
        days: { type: "integer", description: "往回搜多少天，1-30，默认 7。", minimum: 1, maximum: 30, default: 7 },
        limit: { type: "integer", description: "返回条数，1-20，默认 10。", minimum: 1, maximum: 20, default: 10 },
        lang: { type: "string", description: "返回语言：zh 中文（默认）或 en 英文。", enum: ["zh", "en"], default: "zh" },
      },
    },
  },
];

function clampInt(value, fallback, min, max) {
  const n = Number.parseInt(value, 10);
  if (Number.isNaN(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function resolveLang(args) {
  const raw = args && typeof args.lang === "string" ? args.lang.trim().toLowerCase() : "";
  return raw === "en" || raw.startsWith("en-") || raw.startsWith("en_") ? "en" : "zh";
}

function dataPrefix(lang) {
  return lang === "en" ? "/en/data" : "/data";
}

function shapeItem(item, lang) {
  const titleKey = lang === "en" ? "en_title" : "zh_title";
  const onelinerKey = lang === "en" ? "en_oneliner" : "zh_oneliner";
  const out = {
    title: item[titleKey] || item.title,
    oneliner: item[onelinerKey] || "",
    link: item.link,
    source: item.source,
    published: item.published,
    category: item.category_label || item.category,
    score: item.score,
  };
  if (item[titleKey] && item.title && item[titleKey] !== item.title) {
    out.original_title = item.title;
  }
  return out;
}

async function fetchJson(path) {
  const res = await fetch(`${DATA_ORIGIN}${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`上游数据 ${path} 返回 HTTP ${res.status}`);
  return res.json();
}

async function callGetHot(args) {
  const lang = resolveLang(args);
  const limit = clampInt(args.limit, 10, 1, 20);
  const data = await fetchJson(`${dataPrefix(lang)}/hot.json`);
  const hot = Array.isArray(data.hot) ? data.hot : [];
  return {
    lang,
    updated_at: data.generated_at_cst || data.generated_at,
    total_7d: data.counts ? data.counts.total_7d : undefined,
    items: hot.slice(0, limit).map((item) => shapeItem(item, lang)),
  };
}

async function callGetDaily(args) {
  const lang = resolveLang(args);
  const data = await fetchJson(`${dataPrefix(lang)}/daily.json`);
  const items = Array.isArray(data.items) ? data.items : [];
  return {
    lang,
    date: data.label || data.date_iso,
    issue: data.issue,
    summary: data.summary,
    items: items.map((item) => shapeItem(item, lang)),
  };
}

async function callSearchItems(args) {
  const lang = resolveLang(args);
  const prefix = dataPrefix(lang);
  const query = typeof args.query === "string" ? args.query.trim().toLowerCase() : "";
  const category = typeof args.category === "string" ? args.category.trim() : "";
  const days = clampInt(args.days, 7, 1, 30);
  const limit = clampInt(args.limit, 10, 1, 20);
  const index = await fetchJson(`${prefix}/days/index.json`);
  const dayList = Array.isArray(index.days) ? index.days.slice(0, days) : [];
  const settled = await Promise.all(
    dayList.map((d) =>
      fetchJson(`${prefix}/days/${d.date}.json`)
        .then((day) => (Array.isArray(day.items) ? day.items : []))
        .catch(() => [])
    )
  );
  let items = settled.flat();
  if (category) items = items.filter((it) => it.category === category);
  if (query) {
    const titleKey = lang === "en" ? "en_title" : "zh_title";
    const onelinerKey = lang === "en" ? "en_oneliner" : "zh_oneliner";
    items = items.filter((it) =>
      [it[titleKey], it.title, it[onelinerKey], it.source]
        .filter(Boolean)
        .some((text) => text.toLowerCase().includes(query))
    );
  }
  items.sort(
    (a, b) => (b.score || 0) - (a.score || 0) || String(b.published).localeCompare(String(a.published))
  );
  return {
    lang,
    query: query || undefined,
    category: category || undefined,
    days_searched: dayList.length,
    matched: items.length,
    items: items.slice(0, limit).map((item) => shapeItem(item, lang)),
  };
}

async function callTool(name, args) {
  const input = args && typeof args === "object" ? args : {};
  switch (name) {
    case "get_hot":
      return callGetHot(input);
    case "get_daily":
      return callGetDaily(input);
    case "search_items":
      return callSearchItems(input);
    default:
      throw new Error(`未知工具：${name}`);
  }
}

function rpcResult(id, result) {
  return { jsonrpc: "2.0", id, result };
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

async function handleMessage(msg) {
  // Returns a response object, or null for notifications (no reply).
  if (!msg || typeof msg !== "object" || Array.isArray(msg)) {
    return rpcError(null, -32600, "无效的 JSON-RPC 请求");
  }
  const isNotification = !("id" in msg) || msg.id === undefined;
  if (isNotification) return null; // e.g. notifications/initialized: no reply
  const id = msg.id;
  const method = msg.method;
  try {
    switch (method) {
      case "initialize": {
        const requested = msg.params && msg.params.protocolVersion;
        const protocolVersion = KNOWN_PROTOCOLS.includes(requested)
          ? requested
          : KNOWN_PROTOCOLS[0];
        return rpcResult(id, {
          protocolVersion,
          capabilities: { tools: {} },
          serverInfo: SERVER_INFO,
        });
      }
      case "ping":
        return rpcResult(id, {});
      case "tools/list":
        return rpcResult(id, { tools: TOOLS });
      case "tools/call": {
        const params = msg.params || {};
        try {
          const payload = await callTool(params.name, params.arguments);
          return rpcResult(id, {
            content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
            structuredContent: payload,
            isError: false,
          });
        } catch (err) {
          return rpcResult(id, {
            content: [{ type: "text", text: `工具调用失败：${err.message}` }],
            isError: true,
          });
        }
      }
      default:
        return rpcError(id, -32601, `未知方法：${method}`);
    }
  } catch (err) {
    return rpcError(id, -32603, `内部错误：${err.message}`);
  }
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS, DELETE",
    "Access-Control-Allow-Headers": "Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version",
    "Access-Control-Expose-Headers": "Mcp-Session-Id",
  };
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

async function handleMcp(request) {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }
  if (request.method === "GET" || request.method === "DELETE") {
    // Stateless server: no SSE streams, no sessions.
    return jsonResponse(
      {
        jsonrpc: "2.0",
        id: null,
        error: {
          code: -32600,
          message:
            "IndieHOT MCP 端点只接受 POST（Streamable HTTP，无会话）。请用 POST 发送 JSON-RPC 请求，详见 https://staroltest.github.io/indie-game-hot/about.html",
        },
      },
      405
    );
  }
  if (request.method !== "POST") {
    return jsonResponse(
      { jsonrpc: "2.0", id: null, error: { code: -32600, message: `不支持的方法：${request.method}` } },
      405
    );
  }
  let body;
  try {
    body = await request.json();
  } catch {
    return jsonResponse(rpcError(null, -32700, "请求体不是合法 JSON"), 400);
  }
  if (Array.isArray(body)) {
    const responses = [];
    for (const msg of body) {
      const res = await handleMessage(msg);
      if (res) responses.push(res);
    }
    if (responses.length === 0) return new Response(null, { status: 202, headers: corsHeaders() });
    return jsonResponse(responses);
  }
  const response = await handleMessage(body);
  if (!response) return new Response(null, { status: 202, headers: corsHeaders() });
  return jsonResponse(response);
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/mcp" || url.pathname.startsWith("/mcp/")) {
      return handleMcp(request);
    }
    // _routes.json keeps every other path on static serving; reaching here
    // means routing misfired, so fail closed instead of shadowing the site.
    return jsonResponse(
      { jsonrpc: "2.0", id: null, error: { code: -32601, message: "MCP 端点仅在 /mcp 提供" } },
      404
    );
  },
};
