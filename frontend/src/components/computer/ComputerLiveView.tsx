import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type RFB from "@novnc/novnc";
import { Maximize2Icon, Minimize2Icon, MinusIcon, PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

export default function ComputerLiveView({ url, viewOnly, name, controls }: {
  url: string;
  viewOnly: boolean;
  name: string;
  controls?: ReactNode;
}) {
  const inlineViewport = useRef<HTMLDivElement>(null);
  const expandedViewport = useRef<HTMLDivElement>(null);
  const screen = useRef<HTMLDivElement | null>(null);
  const connection = useRef<RFB | null>(null);
  const layout = useRef({ expanded: false, zoom: null as number | null, width: 1280, height: 800 });
  const [expanded, setExpanded] = useState(false);
  const [zoom, setZoom] = useState<number | null>(null);
  const [status, setStatus] = useState("Connecting to the desktop…");

  const arrangeScreen = useCallback(() => {
    const viewport = layout.current.expanded ? expandedViewport.current : inlineViewport.current;
    const host = screen.current;
    if (!viewport || !host) return;
    // Move the same noVNC target into the dialog. Expanding or zooming must not
    // open a second connection, lose keyboard focus state, or change control.
    if (host.parentElement !== viewport) viewport.appendChild(host);
    const { zoom: percent, width, height } = layout.current;
    host.style.width = `${percent === null ? viewport.clientWidth : width * percent / 100}px`;
    host.style.height = `${percent === null ? viewport.clientHeight : height * percent / 100}px`;
    host.style.marginInline = "auto";
    // Let noVNC perform the actual scaling, including input coordinate mapping.
    // CSS transforms on the canvas would send clicks to incorrect coordinates.
    if (connection.current) connection.current.scaleViewport = true;
  }, []);

  useLayoutEffect(() => {
    layout.current.expanded = expanded;
    layout.current.zoom = zoom;
    arrangeScreen();
    const observer = new ResizeObserver(arrangeScreen);
    for (const viewport of [inlineViewport.current, expandedViewport.current]) {
      if (viewport) observer.observe(viewport);
    }
    return () => observer.disconnect();
  }, [expanded, zoom, arrangeScreen]);

  useEffect(() => {
    let disposed = false;
    let disconnect: (() => void) | undefined;
    const host = document.createElement("div");
    screen.current = host;
    arrangeScreen();
    const dimensions = new MutationObserver(() => {
      const canvas = host.querySelector("canvas");
      if (canvas?.width && canvas.height
          && (layout.current.width !== canvas.width || layout.current.height !== canvas.height)) {
        layout.current.width = canvas.width;
        layout.current.height = canvas.height;
        arrangeScreen();
      }
    });
    dimensions.observe(host, { childList: true, subtree: true, attributes: true, attributeFilter: ["width", "height"] });
    setStatus("Connecting to the desktop…");
    void import("@novnc/novnc").then(({ default: RFB }) => {
      if (disposed) return;
      const socket = new URL(url, window.location.origin);
      socket.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const rfb = new RFB(host, socket.toString(), { wsProtocols: ["binary"] });
      connection.current = rfb;
      let ended = false;
      disconnect = () => { if (!ended) { ended = true; rfb.disconnect(); } };
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.viewOnly = viewOnly;
      rfb.addEventListener("connect", () => {
        if (!disposed) { arrangeScreen(); setStatus("Live desktop connected"); }
      });
      rfb.addEventListener("disconnect", () => {
        ended = true;
        if (!disposed) setStatus("Desktop disconnected. Use Reconnect to connect again.");
      });
      rfb.addEventListener("securityfailure", () => {
        if (!disposed) setStatus("Desktop access expired or unavailable. Use Reconnect.");
      });
    }).catch(() => {
      if (!disposed) setStatus("Couldn’t connect to the desktop. Use Reconnect to try again.");
    });
    return () => {
      disposed = true;
      dimensions.disconnect();
      disconnect?.();
      connection.current = null;
      screen.current = null;
      host.remove();
    };
  }, [url, viewOnly, arrangeScreen]);

  const changeZoom = (direction: -1 | 1) => {
    const canvas = screen.current?.querySelector("canvas");
    const current = zoom ?? (canvas?.width ? canvas.getBoundingClientRect().width / canvas.width * 100 : 100);
    const next = direction === 1 ? Math.floor(current / 25) * 25 + 25 : Math.ceil(current / 25) * 25 - 25;
    setZoom(Math.min(200, Math.max(25, next)));
  };

  const toolbar = (large: boolean) => <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Desktop size">
    <Button variant="chip" size="chip" aria-label="Zoom out desktop" title="Zoom out desktop" disabled={zoom === 25} onClick={() => changeZoom(-1)}><MinusIcon /></Button>
    <span className="min-w-9 text-center text-xs tabular-nums" aria-live="polite">{zoom === null ? "Fit" : `${zoom}%`}</span>
    <Button variant="chip" size="chip" aria-label="Zoom in desktop" title="Zoom in desktop" disabled={zoom === 200} onClick={() => changeZoom(1)}><PlusIcon /></Button>
    <Button variant="chip" size="chip" aria-pressed={zoom === null} title="Fit the whole desktop in the viewer" onClick={() => setZoom(null)}>Fit</Button>
    <Button variant="chip" size="chip" aria-pressed={zoom === 100} title="Show the desktop at its original size" onClick={() => setZoom(100)}>100%</Button>
    <Button variant="chip" size="chip" className="ml-auto" onClick={() => setExpanded(!large)} aria-label={large ? "Close expanded desktop" : "Expand desktop"}>
      {large ? <Minimize2Icon /> : <Maximize2Icon />}{large ? "Collapse" : "Expand"}
    </Button>
  </div>;

  return (
    <div className="flex flex-col gap-2">
      {toolbar(false)}
      <div ref={inlineViewport} role="region" aria-label={`${name}'s live desktop`}
        className="aspect-[16/10] w-full overflow-auto rounded-lg bg-[#181818]" />
      <p role="status" className="text-muted-foreground text-xs">{status}</p>
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent showCloseButton={false} className="flex h-[calc(100dvh-1rem)] w-[calc(100vw-1rem)] max-w-none flex-col gap-3 overflow-hidden p-3 sm:max-w-none">
          <DialogHeader className="gap-1 text-left">
            <DialogTitle>{name}'s live desktop</DialogTitle>
            <DialogDescription>{viewOnly ? "View only. Take over to use the mouse and keyboard." : "You control the mouse and keyboard. Hand back when finished."}</DialogDescription>
          </DialogHeader>
          {toolbar(true)}
          <div ref={(element) => { expandedViewport.current = element; if (element) arrangeScreen(); }}
            role="region" aria-label="Expanded live desktop" className="min-h-0 flex-1 overflow-auto rounded-lg bg-[#181818]" />
          <div className="flex shrink-0 flex-col gap-2">
            <p role="status" className="text-muted-foreground text-xs">{status}</p>
            {controls}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
