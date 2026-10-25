// Isolated API, worker and Vite; no shell-specific process management.
import { mkdtempSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const backend = resolve(frontend, "..", "backend");
const run = mkdtempSync(resolve(tmpdir(), "band-provider-e2e-"));
const env = { ...process.env, DATABASE_URL: `sqlite:///${run.replaceAll("\\", "/")}/app.db`,
  AGENT_CHECKPOINT_PATH: resolve(run, "checkpoints.db"), DOCUMENTS_PATH: resolve(run, "documents"),
  APP_SECRET: "e2e-app-secret-e2e-app-secret-32b", BAND_ENV: "test", AUTH_ATTEMPT_LIMIT: "100000",
  RECOVERY_DELIVERY: "memory", E2E_FAKE_AGENT: "1", BAND_E2E_API_PORT: "18019" };
for (const key of ["BAND_USER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "TAVILY_API_KEY", "CURSOR_API_KEY", "E2B_API_KEY", "TENKI_API_KEY", "TENKI_AUTH_TOKEN", "TENKI_DESKTOP_IMAGE", "EXA_API_KEY", "COMPOSIO_API_KEY", "MODEL_PRICES", "OPENCLAW_GATEWAY_URLS", "PROVIDER_ENDPOINT_ALLOWLIST"]) env[key] = "";
mkdirSync(env.DOCUMENTS_PATH, { recursive: true });
const children = [];
function start(command, args, cwd) {
  const child = spawn(command, args, { cwd, env, stdio: "inherit", windowsHide: true });
  children.push(child);
  child.on("error", (error) => { console.error(error); stop(1); });
  child.on("exit", (code) => { if (!stopping) stop(code ?? 1); });
  return child;
}
let stopping = false;
function stop(code = 0) {
  if (stopping) return; stopping = true;
  for (const child of children) {
    if (process.platform === "win32") spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    else child.kill("SIGTERM");
  }
  setTimeout(() => process.exit(code), 1000);
}
process.on("SIGTERM", () => stop()); process.on("SIGINT", () => stop());
// Test-only Agent API stub. The production app never accepts fake keys.
const apiServer = `
import uvicorn
from fastapi import WebSocket
from app.main import app
from app.band import connections
from app.band.listener import BandIdentity
import sys
sys.path.insert(0, "tests")
from fake_band_platform import OfflineBandNetwork
import app.band.platform as platform
import app.band.accounts as accounts
import app.workspace.service as service
network = OfflineBandNetwork()
platform.AsyncRestClient = network.client
connections.AsyncRestClient = network.client
accounts.AsyncRestClient = network.management_client
service.PlatformListener = network.listener
async def identity(key):
    if not key.startswith("band_a_"):
        raise ValueError("Invalid offline test key")
    return BandIdentity(id=key.removeprefix("band_a_"), name="E2E Band connection", handle="e2e-band")
connections.fetch_identity = identity
service.fetch_identity = identity
async def start_agent(manager, profile, *, owner_id=""):
    manager._band_ids[profile.id] = profile.band_agent_id
    manager.status[profile.id] = {"state": "online", "error": None}
service.AgentManager.start = start_agent
@app.websocket("/api/e2e/desktop-transport")
async def desktop_transport(socket: WebSocket):
    await socket.accept(subprotocol="binary")
    await socket.send_bytes(b"RFB 003.008\\n")
    await socket.send_bytes(await socket.receive_bytes())
    await socket.close()
uvicorn.run(app, host="127.0.0.1", port=${Number(env.BAND_E2E_API_PORT)})
`;
start("uv", ["run", "python", "-c", apiServer], backend);
start("uv", ["run", "python", "-c", `
import sys, asyncio
sys.path.insert(0, "tests")
from app.persistence.models import init_db
from fake_band_platform import OfflineBandNetwork
import app.band.connections as connections
connections.AsyncRestClient = OfflineBandNetwork().client
from app.tasks.worker import work
asyncio.run(work())
`], backend);
let ready = false;
for (let attempt = 0; attempt < 120 && !stopping; attempt++) {
  try { ready = (await fetch(`http://127.0.0.1:${env.BAND_E2E_API_PORT}/api/health`)).ok; } catch { /* starting */ }
  if (ready) break;
  await new Promise((resolve) => setTimeout(resolve, 500));
}
if (!ready) stop(1);
else start(process.execPath, [resolve(frontend, "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", "18173", "--strictPort"], frontend);
