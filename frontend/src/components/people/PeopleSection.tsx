import type { ReactNode } from "react";

/** Runs a people action, refreshes the lists and reports failures. */
export type RunAction = (action: () => Promise<void>) => Promise<void>;

export default function PeopleSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold">{title}</h2>
      {children}
    </section>
  );
}
