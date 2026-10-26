import wasmUrl from "shiki/onig.wasm?url";

export default async function instantiate(imports: WebAssembly.Imports) {
  const response = await fetch(wasmUrl);
  if (!response.ok) throw new Error("Could not load syntax highlighting engine");
  return WebAssembly.instantiate(await response.arrayBuffer(), imports);
}
