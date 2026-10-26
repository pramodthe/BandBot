import { useEffect, useRef, useState, type ComponentProps } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import AgentProfile from "./AgentProfile";

type Props = ComponentProps<typeof AgentProfile> & { onClose: () => void };

export default function AgentProfilePanel(props: Props) {
  const [inline, setInline] = useState(() => window.matchMedia("(min-width: 1280px)").matches);
  const previousFocus = useRef(document.activeElement);
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    const query = window.matchMedia("(min-width: 1280px)");
    const update = () => setInline(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (inline) panel.current?.focus();
  }, [inline]);
  useEffect(() => () => {
    const target = previousFocus.current;
    if (target instanceof HTMLElement && target.isConnected) target.focus({ preventScroll: true });
  }, []);
  const content = <AgentProfile {...props} embedded />;
  if (inline) return <aside ref={panel} tabIndex={-1} aria-label={`${props.agent.name} profile panel`}
    className="border-border bg-background flex w-[380px] min-w-0 shrink-0 flex-col border-l outline-none"
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); props.onClose(); } }}>
    {content}
  </aside>;
  return <Dialog open onOpenChange={(open) => !open && props.onClose()}>
    <DialogContent showCloseButton={false} className="top-0 right-0 bottom-0 left-auto flex h-dvh max-h-dvh w-[400px] max-w-full translate-x-0 translate-y-0 flex-col gap-0 rounded-none border-y-0 border-r-0 p-0 sm:max-w-[400px] narrow:w-full narrow:max-w-full narrow:sm:max-w-full"
      onCloseAutoFocus={(event) => { event.preventDefault(); const target = previousFocus.current; if (target instanceof HTMLElement && target.isConnected) target.focus({ preventScroll: true }); }}>
      <DialogHeader className="sr-only"><DialogTitle>{props.agent.name} profile</DialogTitle><DialogDescription>Agent overview, skills, connected apps, and channel computers.</DialogDescription></DialogHeader>
      {content}
    </DialogContent>
  </Dialog>;
}
