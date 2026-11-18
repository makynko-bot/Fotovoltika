/**
 * Cloudflare Worker – okamžité hodnoty zo SEMS+ pre stránku „Moja fotovoltika“.
 *
 * GET /  ->  {"time":"2026-11-18T19:15:03Z","pv":1.2,"load":0.8,"grid":-0.4,"bat":0,"soc":77}
 *            (výkony v kW, grid > 0 = odber zo siete, bat > 0 = vybíjanie, soc v %)
 *
 * Tajné premenné (Settings → Variables and Secrets, typ „Secret“):
 *   SEMS_ACCOUNT, SEMS_PASSWORD  (SEMS_STATION_ID nepovinné – inak prvá elektráreň na účte)
 * Voliteľná premenná:
 *   ALLOWED_ORIGIN – adresa stránky, napr. https://meno.github.io (predvolene * = ktokoľvek)
 */

const LOGIN_URL = "https://semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login";
const GATEWAY_URL = "https://eu-gateway.semsportal.com/web/sems";
const OK = [0, "0", "00000"];
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const CACHE_SECONDS = 20; // viac návštevníkov naraz = stále len jedna požiadavka na SEMS

// Stav sa drží v pamäti inštancie (prežije medzi požiadavkami, kým inštancia beží).
let session = null;          // { token, apiBase }
let cached = null;           // { at, body }
let station = null;          // ID elektrárne (zistí sa automaticky)

async function stationId(env) {
  if (env.SEMS_STATION_ID) return env.SEMS_STATION_ID;
  if (!station) {
    const data = await semsGet(env, "/sems-plant/api/portal/stations/page", { current: 1, size: 100 });
    const first = ((data && data.dataList) || []).find((s) => s && s.id);
    if (!first) throw new Error("Na účte sa nenašla žiadna elektráreň");
    station = String(first.id);
  }
  return station;
}

export default {
  async fetch(request, env) {
    const cors = {
      "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Cache-Control": `public, max-age=${CACHE_SECONDS}`,
      "Content-Type": "application/json; charset=utf-8",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "GET") return new Response('{"error":"method"}', { status: 405, headers: cors });

    try {
      if (!cached || Date.now() - cached.at > CACHE_SECONDS * 1000) {
        const station = await stationId(env);
        const flow = await semsGet(env, `/sems-plant/api/stations/flow?stationId=${encodeURIComponent(station)}`);
        cached = { at: Date.now(), body: JSON.stringify(normalize(flow || {})) };
      }
      return new Response(cached.body, { headers: cors });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e.message || e) }), { status: 502, headers: cors });
    }
  },
};

function num(v) {
  const n = v == null ? NaN : Number(v);
  return Number.isFinite(n) ? n : null;
}

function normalize(f) {
  return {
    time: new Date().toISOString(),
    pv: num(f.pSystem ?? f.pAc),
    load: num(f.pConsum),
    grid: f.pGrid == null ? null : -num(f.pGrid) || 0,   // SEMS+: kladné = dodávka; otočíme na kladné = odber
    bat: num(f.pBat),
    soc: num(f.soc),
  };
}

// ------------------------------------------------------------------ SEMS+ klient

async function signature(token) {
  const ts = Date.now();
  const data = new TextEncoder().encode(`${ts}@${token.uid || ""}@${token.token || ""}`);
  const hex = [...new Uint8Array(await crypto.subtle.digest("SHA-256", data))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  return btoa(`${hex}@${ts}`);
}

async function login(env) {
  if (!env.SEMS_ACCOUNT || !env.SEMS_PASSWORD) {
    throw new Error("Chýbajú SEMS_ACCOUNT / SEMS_PASSWORD");
  }
  const r = await fetch(LOGIN_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": "application/json, text/plain, */*",
      "Origin": "https://semsplus.goodwe.com",
      "Referer": "https://semsplus.goodwe.com/",
      "Token": JSON.stringify({ uid: "", timestamp: 0, token: "", client: "semsPlusWeb", version: "", language: "en" }),
      "X-Signature": await signature({}),
      "User-Agent": UA,
    },
    body: JSON.stringify({ account: env.SEMS_ACCOUNT, pwd: btoa(md5(env.SEMS_PASSWORD)), agreement: 1, isChinese: false, isLocal: false }),
  });
  if (!r.ok) throw new Error(`Prihlásenie: HTTP ${r.status}`);
  const body = await r.json();
  if (!OK.includes(body.code) || !body.data || !body.data.token) {
    throw new Error(`Prihlásenie zlyhalo: ${body.code} ${body.msg || ""}`);
  }
  const api = body.api || body.data.api;
  const apiBase = typeof api === "string" && api.startsWith("https://") ? api.replace(/\/+$/, "") : GATEWAY_URL;
  session = { token: { ...body.data, api: apiBase }, apiBase };
}

async function semsGet(env, path, postBody) {
  if (!session) await login(env);
  for (let attempt = 0; attempt < 2; attempt++) {
    const r = await fetch(session.apiBase + path, {
      method: postBody ? "POST" : "GET",
      body: postBody ? JSON.stringify(postBody) : undefined,
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
        "Token": JSON.stringify(session.token),
        "X-Signature": await signature(session.token),
      },
    });
    if (r.status === 401 && attempt === 0) { await login(env); continue; }
    if (!r.ok) throw new Error(`SEMS: HTTP ${r.status}`);
    const body = await r.json();
    if (body.code === "C0602" && attempt === 0) { await login(env); continue; } // vypršaný token
    if (!OK.includes(body.code)) throw new Error(`SEMS: ${body.code} ${body.msg || ""}`);
    return body.data;
  }
  throw new Error("SEMS: obnovenie prihlásenia zlyhalo");
}

// ------------------------------------------------------------------ MD5 (Web Crypto ho nemá)

function md5(str) {
  const bytes = new TextEncoder().encode(str);
  const len = bytes.length;
  const words = new Uint32Array((((len + 8) >>> 6) + 1) * 16);
  for (let i = 0; i < len; i++) words[i >> 2] |= bytes[i] << ((i % 4) * 8);
  words[len >> 2] |= 0x80 << ((len % 4) * 8);
  words[words.length - 2] = len * 8;
  const S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
  const K = Array.from({ length: 64 }, (_, i) => Math.floor(Math.abs(Math.sin(i + 1)) * 2 ** 32) >>> 0);
  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
  for (let o = 0; o < words.length; o += 16) {
    let a = a0, b = b0, c = c0, d = d0;
    for (let i = 0; i < 64; i++) {
      let f, g;
      if (i < 16) { f = (b & c) | (~b & d); g = i; }
      else if (i < 32) { f = (d & b) | (~d & c); g = (5 * i + 1) % 16; }
      else if (i < 48) { f = b ^ c ^ d; g = (3 * i + 5) % 16; }
      else { f = c ^ (b | ~d); g = (7 * i) % 16; }
      const s = S[(i >> 4) * 4 + (i % 4)];
      const t = d; d = c; c = b;
      const x = (a + f + K[i] + words[o + g]) >>> 0;
      b = (b + ((x << s) | (x >>> (32 - s)))) >>> 0;
      a = t;
    }
    a0 = (a0 + a) >>> 0; b0 = (b0 + b) >>> 0; c0 = (c0 + c) >>> 0; d0 = (d0 + d) >>> 0;
  }
  return [a0, b0, c0, d0].map((v) => [0, 8, 16, 24].map((s) => ((v >>> s) & 255).toString(16).padStart(2, "0")).join("")).join("");
}

