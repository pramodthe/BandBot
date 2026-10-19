import { cn } from "@/lib/utils";

const FILLS: Record<string, string> = {
  online: "border-online bg-online",
  starting: "border-away bg-away",
  error: "border-destructive bg-destructive",
};

export default function StatusDot({ state, title }: { state: string; title?: string }) {
  return (
    <span
      title={title}
      className={cn("inline-block size-[9px] shrink-0 rounded-full border-[1.5px] border-[#9a8a9a]", FILLS[state])}
    />
  );
}
