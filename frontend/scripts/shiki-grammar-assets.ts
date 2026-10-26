import { pathToFileURL } from "node:url";
import type { Plugin } from "vite";

// These grammars are large JSON data, not executable code. Keep every language
// available, but fetch these two as cacheable assets only when Shiki requests them.
export function shikiGrammarAssets(): Plugin {
  return {
    name: "shiki-grammar-assets",
    apply: "build",
    load: {
      filter: { id: /[\\/]@shikijs[\\/]langs[\\/]dist[\\/](cpp|emacs-lisp)\.mjs$/ },
      async handler(id) {
        const name = /([^\\/]+)\.mjs$/.exec(id)![1];
        const grammar = await import(pathToFileURL(id).href);
        const reference = this.emitFile({
          type: "asset",
          name: `grammar-${name}.json`,
          source: JSON.stringify(grammar.default),
        });
        return `const response = await fetch(import.meta.ROLLUP_FILE_URL_${reference});
if (!response.ok) throw new Error("Could not load syntax grammar");
export default await response.json();`;
      },
    },
  };
}
