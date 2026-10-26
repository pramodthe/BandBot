import assert from "node:assert/strict";
import { readFile, access } from "node:fs/promises";
import test from "node:test";
import { elementSchemas, elementModules } from "../src/components/generated/element-schemas.mjs";

test("every agent extension has a local renderer and rejects host callbacks", async () => {
  const fixtures = JSON.parse(await readFile(new URL("./fixtures/element-props.json", import.meta.url), "utf8"));
  assert.equal(Object.keys(elementSchemas).length, 20);
  assert.deepEqual(Object.keys(elementSchemas).sort(), Object.keys(elementModules).sort());
  for (const [name, module] of Object.entries(elementModules)) {
    assert.equal(elementSchemas[name].safeParse(fixtures[name]).success, true, `${name} fixture`);
    await access(new URL(`../src/components/assistant-ui/elements/${module}.tsx`, import.meta.url));
    for (const forbidden of ["onClick", "onApply", "onOpen", "className", "style", "dangerouslySetInnerHTML", "_action"]) {
      assert.equal(forbidden in elementSchemas[name].shape, false, `${name}.${forbidden}`);
    }
  }
  assert.equal(elementSchemas.NumberTicker.safeParse({ value: 12, label: "Tasks", onClick: "send_email" }).success, false);
  assert.equal(elementSchemas.LinkPreview.safeParse({ href: "javascript:alert(1)" }).success, false);
  assert.equal(elementSchemas.ImageGallery.safeParse({ images: [{ id: "1", src: "https://example.com/1.png", alt: "A" }] }).success, true);
});

test("all 151 upstream entries and 22 compositions are installed without executable specs", async () => {
  const manifest = JSON.parse(await readFile(new URL("../../backend/app/runtime/elements_manifest.json", import.meta.url), "utf8"));
  const examples = JSON.parse(await readFile(new URL("../../backend/app/runtime/ui_examples.json", import.meta.url), "utf8"));
  assert.equal(manifest.length, 151);
  assert.equal(new Set(manifest.map((entry) => entry.id)).size, 151);
  assert.equal(Object.keys(examples).length, 22);
  for (const entry of manifest) {
    for (const path of entry.files) await access(new URL(`../src/${path}`, import.meta.url));
  }
  for (const example of Object.values(examples)) {
    assert.equal(typeof example.ui._type, "string");
    assert.ok(example.source.startsWith("https://www.assistant-ui.com/elements/"));
  }
});
