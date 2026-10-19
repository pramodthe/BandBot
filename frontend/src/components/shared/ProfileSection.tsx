import type { ReactNode } from "react";

export default function ProfileSection({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="border-border flex flex-col gap-3 border-t py-5 first:border-t-0">
      <header className="flex min-h-8 items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {action}
      </header>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

export function ProfileRow({ label, icon, children }: { label: string; icon: ReactNode; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[108px_minmax(0,1fr)] gap-3 py-1.5">
      <dt className="text-muted-foreground flex items-center gap-1.5 [&_svg]:size-3.5 [&_svg]:shrink-0 [&_svg]:stroke-[1.4]">{icon}{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}
