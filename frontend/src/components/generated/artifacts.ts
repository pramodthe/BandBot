import type { Artifact } from "@/types";

export const isHtmlArtifact = (path: string) => /\.html?$/i.test(path);
export const artifactName = (path: string) => path.split(/[\\/]/).pop() || "artifact.txt";

// Opaque-origin frame: scripts can run without reaching workspace storage or APIs.
export const ARTIFACT_SANDBOX = "allow-scripts allow-forms";
const PREVIEW_CSP = "default-src 'none'; script-src 'unsafe-inline' https:; style-src 'unsafe-inline' https:; img-src https: data: blob:; font-src https: data:; media-src https: data: blob:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri about:; form-action 'none'";

export function artifactDocument(content: string): string {
  // The policy precedes the generated document. Subsequent policies cannot relax it.
  // The trusted first base keeps #section links inside srcdoc, not the workspace.
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}"><base href="about:srcdoc"><meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body>${content}</body></html>`;
}

export function downloadArtifact(artifact: Artifact): void {
  const url = URL.createObjectURL(new Blob([artifact.content], { type: isHtmlArtifact(artifact.path) ? "text/html;charset=utf-8" : "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = artifactName(artifact.path);
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
