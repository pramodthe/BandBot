"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { lazy, Suspense } from "react";
import type { GenerativeUIComponent, GenerativeUILibrary } from "@assistant-ui/generative-ui/react";
import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react";
import GenerativeChart from "./generative-chart";
import { elementLibrary } from "@/components/generated/ElementLibrary";
import type { GeoMapPlace } from "./geo-map";
const GeoMap = lazy(() => import("./geo-map").then((module) => ({ default: module.GeoMap })));
const MermaidDiagram = lazy(() => import("./mermaid-diagram").then((module) => ({ default: module.MermaidDiagram })));

const markdownBase = defaultGenerativeUILibrary.Markdown!;

// The stock controls expose accessible labels; keep those labels visible in a shared conversation too.
function labelled(name: string, group = false): GenerativeUIComponent {
  const base = defaultGenerativeUILibrary[name]!;
  const Render = base.render;
  return { ...base, render: (props) => group ? (
    <div className="flex min-w-0 flex-col gap-2"><p className="text-sm font-medium">{props.label}</p><Render {...props} /></div>
  ) : (
    <label className="flex min-w-0 flex-col gap-1.5 text-sm"><span className="font-medium">{props.label}</span><Render {...props} /></label>
  ) };
}

const pinsOf = (places: unknown): GeoMapPlace[] =>
  (Array.isArray(places) ? places : []).flatMap((place: unknown, index) => {
    if (place === null || typeof place !== "object") return [];
    const { label, lat, lng, detail } = place as Record<string, unknown>;
    return typeof label === "string" &&
      typeof lat === "number" &&
      typeof lng === "number"
      ? [
          {
            id: String(index),
            label,
            lat,
            lng,
            description: typeof detail === "string" ? detail : undefined,
          },
        ]
      : [];
  });

/** `MarkdownTextPrimitive` cannot be reused here: it reads from message-part context, not a prop string. */
export const styledGenerativeUILibrary: GenerativeUILibrary = {
  ...defaultGenerativeUILibrary,
  ...elementLibrary,
  Input: labelled("Input"),
  Select: labelled("Select"),
  DatePicker: labelled("DatePicker"),
  RadioGroup: labelled("RadioGroup", true),
  CheckboxGroup: labelled("CheckboxGroup", true),
  Chart: {
    ...defaultGenerativeUILibrary.Chart!,
    render: GenerativeChart,
  },
  Markdown: {
    properties: markdownBase.properties,
    streamProperties: markdownBase.streamProperties,
    description: "A markdown string, rendered with GitHub-flavored markdown.",
    render: ({ value, children }) => (
      <div data-aui="markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{value ?? ""}</ReactMarkdown>
        {children}
      </div>
    ),
  },
  Diagram: {
    ...defaultGenerativeUILibrary.Diagram!,
    render: ({ code, title }) => (
      <figure data-aui="diagram">
        <Suspense fallback={<p role="status">Loading diagram…</p>}><MermaidDiagram code={typeof code === "string" ? code : ""} /></Suspense>
        {typeof title === "string" || typeof title === "number" ? (
          <figcaption data-aui="diagram-title">{title}</figcaption>
        ) : null}
      </figure>
    ),
  },
  Map: {
    ...defaultGenerativeUILibrary.Map!,
    render: ({ places, route }) => {
      const pins = pinsOf(places);
      return (
        <Suspense fallback={<p role="status">Loading map…</p>}><GeoMap
          data-aui="map"
          className="max-w-none [&_[data-slot=geo-map-coordinates]]:hidden"
          places={pins}
          routes={
            route && pins.length > 1
              ? [
                  {
                    id: "route",
                    points: pins.map(({ lat, lng }) => [lat, lng] as const),
                  },
                ]
              : undefined
          }
        /></Suspense>
      );
    },
  },
};
