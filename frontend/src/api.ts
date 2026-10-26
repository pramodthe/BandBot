// This module has no runtime imports so the offline tests can load it directly.
import type { WorkspaceDocument } from "./types";

const TOKEN_KEY = "band-slack-token";

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (token: string | null) =>
  token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY);

export class ApiError extends Error {
  status: number;
  field?: string;

  constructor(message: string, status: number, field?: string) {
    super(message);
    this.status = status;
    this.field = field;
  }
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d))).join("; ");
  }
  return "Request failed";
}

const errorDetail = (data: unknown) => describe(data && typeof data === "object" && "detail" in data ? data.detail : undefined);

function authorization(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function readJson<T>(response: Response): Promise<T> {
  const contentType = response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
  if (contentType !== "application/json" && !contentType?.endsWith("+json")) {
    throw new ApiError("The server returned a webpage instead of an API response. Restart the API and worker to load the latest app version, then retry.", response.status);
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new ApiError("The server returned an unreadable response. Please retry.", response.status);
  }
  if (!response.ok) throw new ApiError(errorDetail(data), response.status,
    data && typeof data === "object" && "field" in data && typeof data.field === "string" ? data.field : undefined);
  return data as T;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...authorization() },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return readJson<T>(response);
}

interface RequestOptions {
  /** Raw request body, sent as application/octet-stream. */
  body?: Blob;
  signal?: AbortSignal;
  /** Error message to show instead of the server's detail. */
  failure?: string;
}

/** Authenticated request for binary uploads and downloads; failures throw an ApiError. */
export async function request(method: string, path: string, { body, signal, failure }: RequestOptions = {}): Promise<Response> {
  const response = await fetch(path, {
    method,
    body,
    signal,
    headers: { ...(body !== undefined ? { "Content-Type": "application/octet-stream" } : {}), ...authorization() },
  });
  if (!response.ok) {
    throw new ApiError(failure ?? errorDetail(await response.json().catch(() => undefined)), response.status);
  }
  return response;
}

/** Save a blob through the browser's download prompt. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function uploadDocument(channelId: number, file: File): Promise<WorkspaceDocument> {
  const response = await request("POST", `/api/channels/${channelId}/documents?name=${encodeURIComponent(file.name)}`, { body: file });
  return readJson<WorkspaceDocument>(response);
}

const documentResponse = (documentId: string, signal?: AbortSignal) =>
  request("GET", `/api/documents/${encodeURIComponent(documentId)}/download`, {
    signal,
    failure: "This file is unavailable or you no longer have access",
  });

export async function documentBlob(documentId: string, signal?: AbortSignal): Promise<Blob> {
  return (await documentResponse(documentId, signal)).blob();
}

export async function downloadDocument(documentId: string, name?: string): Promise<void> {
  const response = await documentResponse(documentId);
  saveBlob(await response.blob(),
    name ?? /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "")?.[1] ?? "document");
}

export function socketUrl(token: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`;
}

export async function uploadSkill(path: string, file: File): Promise<void> {
  await request("POST", path, { body: file });
}

export async function downloadSkill(skillId: string, name: string): Promise<void> {
  const response = await request("GET", `/api/skills/${encodeURIComponent(skillId)}/export`, {
    failure: "Couldn't download this skill",
  });
  saveBlob(await response.blob(), `${name}.zip`);
}
