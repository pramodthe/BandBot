import type { SandboxedUISurface } from "@/types";

export const UI_SANDBOX = "allow-scripts";
const CDNS = "https://cdn.jsdelivr.net https://esm.sh https://cdnjs.cloudflare.com https://unpkg.com";
export const UI_SANDBOX_CSP = `default-src 'none'; script-src 'unsafe-inline' ${CDNS}; style-src 'unsafe-inline' ${CDNS}; img-src data: blob: https://tile.openstreetmap.org https://a.tile.openstreetmap.org https://b.tile.openstreetmap.org https://c.tile.openstreetmap.org https://basemap.nationalmap.gov https://upload.wikimedia.org https://thumb.wikimedia.org; font-src data: ${CDNS}; media-src data: blob:; connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri about:; form-action 'none'`;

const IMPORTS = {
  three: "https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.module.js",
  "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.180.0/examples/jsm/",
  d3: "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm",
  "chart.js": "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/+esm",
  gsap: "https://cdn.jsdelivr.net/npm/gsap@3.13.0/+esm",
};

const scriptText = (value: string) => value.replace(/<\/script/gi, "<\\/script");
const cssText = (value: string) => value.replace(/<\/style/gi, "<\\/style");

export interface SandboxProposal { id: string; method: "sendPrompt" | "openLink"; text?: string; url?: string }

export function sandboxProposal(data: unknown, nonce: string): SandboxProposal | null {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const value = data as Record<string, unknown>;
  if (value.kind !== "band-sandbox:request" || value.nonce !== nonce || typeof value.id !== "string" || !/^[a-zA-Z0-9_-]{1,64}$/.test(value.id)) return null;
  if (value.method === "sendPrompt" && typeof value.text === "string" && value.text.trim() && value.text.length <= 4000) {
    return { id: value.id, method: "sendPrompt", text: value.text.trim() };
  }
  if (value.method === "openLink" && typeof value.url === "string" && value.url.length <= 2000) {
    try {
      const url = new URL(value.url);
      if (url.protocol === "https:" && !url.username && !url.password) return { id: value.id, method: "openLink", url: url.href };
    } catch { /* A malformed URL has no host capability. */ }
  }
  return null;
}

export function sandboxDocument(surface: SandboxedUISurface, nonce: string, hostOrigin: string): string {
  const bootstrap = `(() => {
    const nonce = ${JSON.stringify(nonce)};
    const hostOrigin = ${JSON.stringify(hostOrigin)};
    const nativePost = parent.postMessage.bind(parent);
    const waiting = new Map();
    let serial = 0;
    const post = (kind, value = {}) => { if (parent !== window) nativePost({ ...value, kind, nonce }, hostOrigin); };
    function propose(method, value) {
      if (parent === window) return Promise.reject(new Error("Open this interface in BAND_BOTS to send a follow-up."));
      if (method === "sendPrompt" && (typeof value.text !== "string" || !value.text.trim() || value.text.length > 4000)) return Promise.reject(new Error("Provide a message of 1-4000 characters."));
      if (method === "openLink") {
        try { const url = new URL(value.url); if (url.protocol !== "https:" || url.username || url.password || url.href.length > 2000) throw new Error(); }
        catch { return Promise.reject(new Error("Provide an HTTPS link without credentials.")); }
      }
      if (waiting.size) return Promise.reject(new Error("An interaction is already awaiting review."));
      const id = "request-" + (++serial);
      return new Promise((resolve, reject) => {
        waiting.set(id, { resolve, reject });
        post("band-sandbox:request", { id, method, ...value });
      });
    }
    const bridge = Object.freeze({
      sendPrompt: ({ text }) => propose("sendPrompt", { text }),
      openLink: ({ url }) => propose("openLink", { url }),
    });
    Object.defineProperty(window, "BandUI", { value: bridge, writable: false, configurable: false });
    Object.defineProperty(window, "Websandbox", { value: Object.freeze({ connection: Object.freeze({ remote: bridge }) }), writable: false, configurable: false });
    window.addEventListener("message", (event) => {
      if (event.source !== parent || event.origin !== hostOrigin || !event.data || event.data.nonce !== nonce) return;
      const data = event.data;
      if (data.kind === "band-sandbox:response") {
        const entry = waiting.get(data.id);
        if (!entry) return;
        waiting.delete(data.id);
        data.ok ? entry.resolve({ status: "confirmed" }) : entry.reject(new Error("The interaction was cancelled or could not be completed."));
      } else if (data.kind === "band-sandbox:theme" && data.colors && typeof data.colors === "object") {
        for (const name of ["background", "foreground", "muted", "muted-foreground", "primary", "primary-foreground", "border", "card", "card-foreground"]) {
          const color = data.colors[name];
          if (typeof color === "string" && color.length < 200 && CSS.supports("color", color)) document.documentElement.style.setProperty("--" + name, color);
        }
        document.documentElement.style.colorScheme = data.dark ? "dark" : "light";
        document.documentElement.dataset.theme = data.dark ? "dark" : "light";
      }
    });
    window.addEventListener("error", () => post("band-sandbox:error"));
    window.addEventListener("unhandledrejection", () => post("band-sandbox:error"));
    window.addEventListener("DOMContentLoaded", () => {
      post("band-sandbox:ready");
      let scheduled = false;
      const resize = () => {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => {
          scheduled = false;
          post("band-sandbox:resize", { height: Math.min(1200, Math.max(240, Math.ceil(document.body.scrollHeight + 24))) });
        });
      };
      new ResizeObserver(resize).observe(document.body);
      resize();
    });
  })();`;
  const theme = `:root{color-scheme:light;--background:#fff;--foreground:#18181b;--muted:#f4f4f5;--muted-foreground:#52525b;--primary:#18181b;--primary-foreground:#fff;--border:#d4d4d8;--card:#fff;--card-foreground:#18181b;--color-text-primary:var(--foreground);--color-text-secondary:var(--muted-foreground);--color-background-primary:var(--background);--color-background-secondary:var(--muted);--color-border-tertiary:var(--border)}*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--background);color:var(--foreground);font:14px/1.5 system-ui,sans-serif;overflow-wrap:anywhere}svg,canvas,img{max-width:100%}button,input,select,textarea{font:inherit}button{cursor:pointer}input,select,textarea{max-width:100%;background:var(--background);color:var(--foreground);border:1px solid var(--border);border-radius:6px;padding:6px 8px}:focus-visible{outline:2px solid var(--primary);outline-offset:3px}@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}`;
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${UI_SANDBOX_CSP}"><base href="about:srcdoc"><meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${theme}</style><script>${scriptText(bootstrap)}</script><script type="importmap">${scriptText(JSON.stringify({ imports: IMPORTS }))}</script><style>${cssText(surface.css)}</style></head><body>${surface.html}<script>${scriptText(surface.jsFunctions)}</script><script>${scriptText(surface.jsExpressions)}</script></body></html>`;
}
