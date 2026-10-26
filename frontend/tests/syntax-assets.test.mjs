import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createOnigurumaEngine } from "shiki/engine/oniguruma";
import { importTypeScript } from "./typescript-module.mjs";

const fingerprint = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");

for (const language of ["cpp", "emacs-lisp"]) {
  test(`${language} retains the complete grammar and embedded languages in its JSON asset`, async (context) => {
    const { shikiGrammarAssets } = await importTypeScript(new URL("../scripts/shiki-grammar-assets.ts", import.meta.url));
    const id = import.meta.resolve(`@shikijs/langs/${language}`);
    const original = await import(id);
    let emitted;
    const plugin = shikiGrammarAssets();
    const code = await plugin.load.handler.call({ emitFile: (asset) => { emitted = asset; return "test"; } },
      fileURLToPath(id));
    assert.equal(emitted.type, "asset");
    assert.equal(emitted.name, `grammar-${language}.json`);
    assert.equal(fingerprint(JSON.parse(emitted.source)), fingerprint(original.default));
    context.mock.method(globalThis, "fetch", async () => new Response(emitted.source));
    const loader = code.replaceAll("import.meta.ROLLUP_FILE_URL_test", JSON.stringify(`/grammar-${language}.json`));
    const loaded = await import(`data:text/javascript;base64,${Buffer.from(loader).toString("base64")}`);
    assert.equal(fingerprint(loaded.default), fingerprint(original.default));
  });
}

test("the standalone WASM loader initializes the real Oniguruma engine", async (context) => {
  const data = await readFile(new URL(import.meta.resolve("shiki/onig.wasm")));
  context.mock.method(globalThis, "fetch", async () => new Response(data));
  const assetModule = `data:text/javascript,export default ${JSON.stringify(pathToFileURL("onig.wasm").href)}`;
  const { default: instantiate } = await importTypeScript(new URL("../src/lib/shiki-wasm.ts", import.meta.url), {
    '"shiki/onig.wasm?url"': JSON.stringify(assetModule),
  });
  const engine = await createOnigurumaEngine({ default: instantiate });
  const scanner = engine.createScanner(["hello"]);
  const result = scanner.findNextMatchSync("hello world", 0);
  assert.equal(result.index, 0);
  scanner.dispose();
});
