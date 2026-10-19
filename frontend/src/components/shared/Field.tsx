import type { ReactNode } from "react";

export default function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-bold">
        {label}
        {hint && <span className="text-muted-foreground font-normal"> {hint}</span>}
      </label>
      {children}
    </div>
  );
}
