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

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = getToken();
  const response = await fetch(path, {
    method,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
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
  if (!response.ok) throw new ApiError(describe(data && typeof data === "object" && "detail" in data ? data.detail : undefined), response.status,
    data && typeof data === "object" && "field" in data && typeof data.field === "string" ? data.field : undefined);
  return data as T;
}

export async function uploadDocument(channelId: number, file: File): Promise<import("./types").WorkspaceDocument> {
  const response = await fetch(`/api/channels/${channelId}/documents?name=${encodeURIComponent(file.name)}`, {
    method: "POST", headers: { Authorization: `Bearer ${getToken() ?? ""}`, "Content-Type": "application/octet-stream" }, body: file,
  });
  const data = await response.json();
  if (!response.ok) throw new ApiError(describe(data.detail), response.status);
  return data;
}

async function documentResponse(documentId: string, signal?: AbortSignal): Promise<Response> {
  const response = await fetch(`/api/documents/${encodeURIComponent(documentId)}/download`, {
    headers: { Authorization: `Bearer ${getToken() ?? ""}` }, signal,
  });
  if (!response.ok) throw new ApiError("This file is unavailable or you no longer have access", response.status);
  return response;
}

export async function documentBlob(documentId: string, signal?: AbortSignal): Promise<Blob> {
  return (await documentResponse(documentId, signal)).blob();
}

export async function downloadDocument(documentId: string, name?: string): Promise<void> {
  const response = await documentResponse(documentId);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name ?? /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") ?? "")?.[1] ?? "document";
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function socketUrl(token: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`;
}

export async function uploadSkill(path: string, file: File): Promise<void> {
  const response = await fetch(path, {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken() ?? ""}`, "Content-Type": "application/octet-stream" },
    body: file,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(describe(data.detail), response.status);
  }
}

export async function downloadSkill(skillId: string, name: string): Promise<void> {
  const response = await fetch(`/api/skills/${encodeURIComponent(skillId)}/export`, {
    headers: { Authorization: `Bearer ${getToken() ?? ""}` },
  });
  if (!response.ok) throw new ApiError("Couldn't download this skill", response.status);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = `${name}.zip`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
