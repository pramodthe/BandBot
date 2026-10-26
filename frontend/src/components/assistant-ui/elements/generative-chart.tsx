"use client";

import { useId, useState } from "react";
import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react";
import { Button } from "../../ui/button";
import { chartTicks } from "../utils/chart-ticks";

const NativeChart = defaultGenerativeUILibrary.Chart!.render;
type Point = { label?: string; value: number };
type Series = { label?: string; data: Point[] };

export default function GenerativeChart(props: Record<string, unknown>) {
  const [showData, setShowData] = useState(false);
  const tableId = useId();
  // Props have passed the catalogue schema; keep the renderer safe during partial streaming too.
  const series: Series[] = Array.isArray(props.series) && props.series.length
    ? props.series.filter((s): s is Series => !!s && Array.isArray(s.data))
    : [{ data: Array.isArray(props.data) ? props.data : [] }];
  const count = Math.max(0, ...series.map((s) => s.data.length));
  const labels = Array.from({ length: count }, (_, i) => series.find((s) => s.data[i]?.label)?.data[i]?.label ?? "");
  const axis = !!props.showAxis && props.variant !== "sparkline";
  const ticks = chartTicks(labels, count, props.variant === "bar");

  return <div className="flex min-w-0 flex-col gap-2">
    <div data-slot="generative-chart" data-axis={axis} className="relative min-w-0">
      <NativeChart {...props} />
      {axis && <div data-slot="generative-chart-axis" aria-hidden="true">
        {ticks.map((tick, i) => <span key={tick.index} title={tick.label} style={{
          left: `${tick.percent}%`, transform: `translateX(${i === 0 ? 0 : i === ticks.length - 1 ? -100 : -50}%)`,
        }}>{tick.label}</span>)}
      </div>}
    </div>
    {count > 0 && <>
      <div className="flex justify-end"><Button variant="ghost" size="sm" aria-controls={tableId} aria-expanded={showData} onClick={() => setShowData((value) => !value)}>{showData ? "Hide chart data" : "Show chart data"}</Button></div>
      {showData && <div id={tableId} className="max-h-72 overflow-auto rounded-lg border">
        <table className="w-full text-left text-xs tabular-nums">
          <caption className="text-muted-foreground p-2 text-left">Chart data · {count} rows</caption>
          <thead><tr className="border-b"><th className="p-2 font-medium">Point</th>{series.map((s, i) => <th key={i} className="p-2 text-right font-medium">{s.label || (series.length === 1 ? "Value" : `Series ${i + 1}`)}</th>)}</tr></thead>
          <tbody>{Array.from({ length: count }, (_, i) => <tr key={i} className="border-b last:border-0"><th scope="row" className="p-2 font-normal">{labels[i] || String(i + 1)}</th>{series.map((s, j) => <td key={j} className="p-2 text-right">{s.data[i]?.value ?? "—"}</td>)}</tr>)}</tbody>
        </table>
      </div>}
    </>}
  </div>;
}
