import { readFile } from "node:fs/promises";
import ts from "typescript";

let serial = 0;

export async function importTypeScript(url, replacements = {}) {
  const source = await readFile(url, "utf8");
  let code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023,
  } }).outputText.replaceAll("import.meta.url", JSON.stringify(url.href));
  for (const [before, after] of Object.entries(replacements)) code = code.replaceAll(before, after);
  return import(`data:text/javascript;base64,${Buffer.from(code + `\n// test ${serial++}`).toString("base64")}`);
}
