import { useState } from "react";
import { DownloadIcon, LayoutDashboardIcon, Maximize2Icon } from "lucide-react";
import type { GeneratedView as ViewDefinition, ViewBlock } from "@/types";
import { cn } from "@/lib/utils";
import { Chart } from "@/components/assistant-ui/elements/chart";
import { DataTable } from "@/components/assistant-ui/elements/data-table";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

function exportTable(block: Extract<ViewBlock, { type: "table" }>, rows: typeof block.rows) {
  const cell = (value: unknown) => {
    let text = String(value ?? "");
    // Keep generated text as text when opened by spreadsheet software.
    if (typeof value === "string" && /^[=+\-@\t\r\n]/.test(text.trimStart())) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  const csv = [block.columns.map((column) => cell(column.label)).join(","), ...rows.map((row) => block.columns.map((column) => cell(row[column.key])).join(","))].join("\r\n");
  const url = URL.createObjectURL(new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${block.title.replace(/[^a-z0-9_-]+/gi, "-").slice(0, 80) || "table"}.csv`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ViewTable({ block }: { block: Extract<ViewBlock, { type: "table" }> }) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const rows = block.rows.filter((row) => !needle || block.columns.some((column) => String(row[column.key] ?? "").toLowerCase().includes(needle)));
  return <section className="flex min-w-0 flex-col gap-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="font-semibold">{block.title}</h4><Button type="button" variant="ghost" size="sm" onClick={() => exportTable(block, rows)} disabled={rows.length === 0}><DownloadIcon data-icon="inline-start" />Export CSV</Button></div>
    <Input aria-label={`Search ${block.title}`} placeholder="Search rows…" value={query} onChange={(event) => setQuery(event.target.value)} className="max-w-sm" />
    <div className="min-w-0 overflow-x-auto"><DataTable caption={block.title} columns={block.columns} rows={rows} emptyMessage="No matching rows." /></div>
    <p className="text-muted-foreground text-xs" aria-live="polite">{rows.length} of {block.rows.length} rows</p>
  </section>;
}

const statusColors = { neutral: "bg-muted-foreground", success: "bg-online", warning: "bg-away", danger: "bg-destructive" };

function Block({ block }: { block: ViewBlock }) {
  switch (block.type) {
    case "text":
      return <section className="flex flex-col gap-2">{block.title && <h4 className="font-semibold">{block.title}</h4>}<p className="text-muted-foreground whitespace-pre-wrap text-sm leading-relaxed break-words">{block.body}</p></section>;
    case "metrics":
      return <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">{block.items.map((metric, index) => <div key={index} className="border-border min-w-0 border-l-2 pl-3"><dt className="text-muted-foreground text-xs break-words">{metric.label}</dt><dd className="mt-1 text-2xl font-semibold tracking-tight break-words">{metric.value}</dd>{metric.detail && <dd className="text-muted-foreground mt-1 text-xs break-words">{metric.detail}</dd>}</div>)}</dl>;
    case "chart":
      return <Chart label={block.label} value={block.value} points={block.points} delta={block.delta} variant={block.variant} visibleCount={block.points.length} />;
    case "table":
      return <ViewTable block={block} />;
    case "list":
      return <section><h4 className="mb-3 font-semibold">{block.title}</h4><ul className="divide-border flex flex-col divide-y">{block.items.map((item, index) => <li key={index} className="flex items-start gap-2.5 py-3 first:pt-0 last:pb-0"><span aria-label={item.status} className={cn("mt-1.5 size-2 shrink-0 rounded-full", statusColors[item.status])} /><div className="min-w-0"><p className="font-medium break-words">{item.label}</p>{item.detail && <p className="text-muted-foreground mt-0.5 text-xs break-words">{item.detail}</p>}</div></li>)}</ul></section>;
    case "progress":
      return <section className="flex flex-col gap-3"><h4 className="font-semibold">{block.title}</h4>{block.items.map((item, index) => <div key={index}><div className="mb-1.5 flex justify-between gap-3 text-xs"><span className="break-words">{item.label}</span><span className="text-muted-foreground shrink-0 tabular-nums">{Math.round(item.value)}%</span></div><div role="progressbar" aria-label={item.label} aria-valuenow={item.value} aria-valuemin={0} aria-valuemax={100} className="bg-muted h-1.5 overflow-hidden rounded-full"><div className="bg-primary h-full rounded-full" style={{ width: `${Math.min(100, Math.max(0, item.value))}%` }} /></div></div>)}</section>;
  }
}

export function ViewBody({ view }: { view: ViewDefinition }) {
  return <div className="flex min-w-0 flex-col gap-6">
    <div className="grid min-w-0 grid-cols-1 gap-6 sm:grid-cols-2">
      {view.blocks.map((block, index) => <div key={index} className={cn("min-w-0", block.type !== "chart" && block.type !== "progress" && "sm:col-span-2")}><Block block={block} /></div>)}
    </div>
    {view.source && <p className="text-muted-foreground border-border border-t pt-3 text-xs break-words">{view.source}</p>}
  </div>;
}

export default function GeneratedView({ view }: { view: ViewDefinition }) {
  const [expanded, setExpanded] = useState(false);
  return <>
    <section aria-label={view.title} className="border-border bg-background overflow-hidden rounded-xl border">
      <header className="border-border flex items-start gap-3 border-b p-5">
        <LayoutDashboardIcon className="text-primary mt-0.5 size-5 shrink-0" />
        <div className="min-w-0 flex-1"><h3 className="font-semibold break-words">{view.title}</h3>{view.description && <p className="text-muted-foreground mt-1 text-[13px] break-words">{view.description}</p>}</div>
        <Button variant="ghost" size="icon-sm" aria-label={`Expand ${view.title}`} onClick={() => setExpanded(true)}><Maximize2Icon /></Button>
      </header>
      <div className="max-h-[640px] overflow-y-auto p-5"><ViewBody view={view} /></div>
      <footer className="border-border flex items-center justify-between gap-3 border-t px-5 py-2"><span className="text-muted-foreground text-xs">Saved in channel Views</span><Button variant="link" size="sm" onClick={() => setExpanded(true)}>Open view</Button></footer>
    </section>
    <Dialog open={expanded} onOpenChange={setExpanded}><DialogContent className="flex max-h-[90dvh] flex-col gap-0 overflow-hidden p-0 sm:max-w-5xl"><DialogHeader className="border-border border-b p-6 pr-12"><DialogTitle>{view.title}</DialogTitle><DialogDescription>{view.description || "Generated view saved with this conversation."}</DialogDescription></DialogHeader><div className="min-h-0 overflow-y-auto p-6"><ViewBody view={view} /></div></DialogContent></Dialog>
  </>;
}
