// Keep the Python agent's contract aligned with the installed assistant-ui catalogue.
import { writeFile, readFile } from "node:fs/promises";
import { z } from "zod";
import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react";
import { elementSchemas } from "../src/components/generated/element-schemas.mjs";

const catalog = Object.fromEntries(Object.entries(defaultGenerativeUILibrary).map(([name, component]) => [
  name, { description: component.description, schema: z.toJSONSchema(component.properties) },
]));
for (const [name, schema] of Object.entries(elementSchemas)) {
  catalog[name] = { description: `${name}: a native Elements display. Supply verified task data; this is a saved snapshot.`, schema: z.toJSONSchema(schema) };
}
const output = new URL("../../backend/app/runtime/ui_catalog.json", import.meta.url);
const packageInfo = JSON.parse(await readFile(new URL("../package.json", import.meta.resolve("@assistant-ui/generative-ui")), "utf8"));
const content = JSON.stringify({ package: "@assistant-ui/generative-ui", version: packageInfo.version, components: catalog }, null, 2) + "\n";
if (process.argv.includes("--check")) {
  if (await readFile(output, "utf8") !== content) throw new Error("UI catalogue is stale. Run npm run ui:catalog.");
} else await writeFile(output, content);
