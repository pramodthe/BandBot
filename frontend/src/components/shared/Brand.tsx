import { cn } from "@/lib/utils";

export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 40" fill="none" className={cn("size-10 shrink-0", className)} aria-hidden="true">
      <rect width="40" height="40" rx="12" fill="#465638" />
      <path d="M12 9h9a5.5 5.5 0 0 1 0 11h-9V9Zm0 11h10a5.5 5.5 0 0 1 0 11H12v-6" stroke="#FAFBF5" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="12" cy="31" r="2" fill="#C5E68A" />
    </svg>
  );
}

export default function Brand({ className }: { className?: string }) {
  return (
    <div className={cn("flex items-center gap-3", className)}>
      <BrandMark />
      <span className="text-xl font-semibold tracking-[-0.04em]">Band Bots<span className="text-primary">.</span></span>
    </div>
  );
}
