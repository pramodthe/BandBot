import assert from "node:assert/strict";
import test from "node:test";
import { importTypeScript } from "./typescript-module.mjs";

const { artifactDocument, artifactName, isHtmlArtifact, ARTIFACT_SANDBOX } = await importTypeScript(
  new URL("../src/components/generated/artifacts.ts", import.meta.url),
);

test("only exported HTML files are treated as executable app previews", () => {
  for (const path of ["/share/app.html", "/share/APP.HTML", "page.htm"]) assert.equal(isHtmlArtifact(path), true);
  for (const path of ["/app.html.js", "app.svg", "/app.html?x=1", "app.tsx", "report.txt"]) assert.equal(isHtmlArtifact(path), false);
  assert.equal(artifactName("/home/oai/share/app.html"), "app.html");
  assert.equal(artifactName("C:\\work\\app.html"), "app.html");
});

test("the isolated preview policy precedes generated code and rejects workspace/network access", () => {
  const html = '<!doctype html><html><head><script>parent.document.body.textContent="changed"</script></head><body><button>Demo</button></body></html>';
  const result = artifactDocument(html);
  assert.ok(result.indexOf('http-equiv="Content-Security-Policy"') < result.indexOf("<script>"));
  for (const directive of ["connect-src 'none'", "frame-src 'none'", "object-src 'none'", "form-action 'none'", "base-uri about:"]) assert.ok(result.includes(directive));
  assert.ok(result.indexOf('<base href="about:srcdoc">') < result.indexOf(html));
  assert.ok(result.includes(html));
  assert.ok(ARTIFACT_SANDBOX.includes("allow-scripts"));
  for (const permission of ["allow-same-origin", "allow-top-navigation", "allow-popups"]) assert.ok(!ARTIFACT_SANDBOX.includes(permission));
});
