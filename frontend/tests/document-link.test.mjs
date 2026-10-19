import assert from "node:assert/strict";
import test from "node:test";
import { importTypeScript } from "./typescript-module.mjs";

const { DOCUMENT_LINK_PATTERN, parseDocumentLink } = await importTypeScript(
  new URL("../src/components/assistant-ui/utils/document-link.ts", import.meta.url),
);
const id = "29defafbd47f47118fee87ac2c708495";

test("document citations and downloads recognize model-added Markdown whitespace", () => {
  for (const padding of ["", " ", "\t", "  \t"]) {
    const link = `[Invoice, page 1](${padding}/api/documents/${id}/download${padding})`;
    assert.deepEqual(`Read ${link}.`.split(new RegExp(`(${DOCUMENT_LINK_PATTERN})`, "g")), ["Read ", link, "."]);
    const parsed = parseDocumentLink(link);
    assert.equal(parsed[1], "Invoice, page 1");
    assert.equal(parsed[2], id);
  }
});

test("document links reject external origins and altered document targets", () => {
  for (const target of [
    `https://example.com/api/documents/${id}/download`,
    `//example.com/api/documents/${id}/download`,
    `/api/documents/${id}/download?redirect=elsewhere`,
    `/api/documents/${id}/download/extra`,
    "/api/documents/invalid/download",
  ]) {
    const link = `[source](${target})`;
    assert.equal(parseDocumentLink(link), null);
    assert.equal(new RegExp(DOCUMENT_LINK_PATTERN).test(link), false);
  }
});
