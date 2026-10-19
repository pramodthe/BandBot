import assert from "node:assert/strict";
import test from "node:test";
import { importTypeScript } from "./typescript-module.mjs";

const { chartTicks } = await importTypeScript(new URL("../src/components/assistant-ui/utils/chart-ticks.ts", import.meta.url));

test("dense axes keep endpoint labels and leave the full chart data intact", () => {
  const labels = Array.from({ length: 100 }, (_, i) => `${i * 18 + 1}–${(i + 1) * 18}`);
  const copy = [...labels];
  const ticks = chartTicks(labels, 100);
  assert.equal(ticks.length, 5);
  assert.deepEqual(ticks[0], { index: 0, label: "1–18", percent: 0 });
  assert.deepEqual(ticks.at(-1), { index: 99, label: "1783–1800", percent: 100 });
  assert.deepEqual(labels, copy);
});

test("empty, single-point and bar axes use finite positions without duplicate ticks", () => {
  assert.deepEqual(chartTicks([], 0), []);
  assert.deepEqual(chartTicks(["Only"], 1), [{ index: 0, label: "Only", percent: 50 }]);
  assert.deepEqual(chartTicks(["A", "B"], 2, true).map((tick) => tick.percent), [25, 75]);
  for (const count of [2, 3, 4, 5, 6, 18, 100, 200]) {
    const ticks = chartTicks([], count);
    assert.equal(new Set(ticks.map((tick) => tick.index)).size, ticks.length);
    assert.ok(ticks.every((tick) => Number.isFinite(tick.percent) && tick.percent >= 0 && tick.percent <= 100));
  }
});
