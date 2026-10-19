import { useEffect, useRef, useState } from "react";

export default function ComputerLiveView({ url, viewOnly, name }: {
  url: string;
  viewOnly: boolean;
  name: string;
}) {
  const screen = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Connecting to the desktop…");

  useEffect(() => {
    let disposed = false;
    let disconnect: (() => void) | undefined;
    setStatus("Connecting to the desktop…");
    void import("@novnc/novnc").then(({ default: RFB }) => {
      if (disposed || !screen.current) return;
      const socket = new URL(url, window.location.origin);
      socket.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const rfb = new RFB(screen.current, socket.toString(), { wsProtocols: ["binary"] });
      let ended = false;
      disconnect = () => { if (!ended) { ended = true; rfb.disconnect(); } };
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.viewOnly = viewOnly;
      rfb.addEventListener("connect", () => {
        if (!disposed) setStatus("Live desktop connected");
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
    return () => { disposed = true; disconnect?.(); };
  }, [url, viewOnly]);

  return (
    <div className="flex flex-col gap-2">
      <div ref={screen} role="region" aria-label={`${name}'s live desktop`}
        className="aspect-[16/10] w-full overflow-hidden rounded-lg bg-[#181818]" />
      <p role="status" className="text-muted-foreground text-xs">{status}</p>
    </div>
  );
}
