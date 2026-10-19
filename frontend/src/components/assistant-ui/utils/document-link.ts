// Accept optional Markdown whitespace, while keeping document targets app-local and exact.
export const DOCUMENT_LINK_PATTERN = "\\[[^\\]\\n]+\\]\\([ \\t]*/api/documents/[a-f0-9]{32}/download[ \\t]*\\)";

export function parseDocumentLink(text: string): RegExpExecArray | null {
  return /^\[([^\]\n]+)\]\([ \t]*\/api\/documents\/([a-f0-9]{32})\/download[ \t]*\)$/.exec(text);
}
