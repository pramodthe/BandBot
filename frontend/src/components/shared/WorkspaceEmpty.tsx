import type { ReactNode } from "react";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";

export default function WorkspaceEmpty({ title, description, icon, children }: { title?: string; description?: string; icon?: ReactNode; children?: ReactNode }) {
  return (
    <Empty className="min-h-0 gap-4 rounded-none border-0 p-6">
      {icon}
      {title ? <EmptyHeader className="max-w-md"><EmptyTitle className="text-2xl font-semibold tracking-tight">{title}</EmptyTitle>{description ? <EmptyDescription>{description}</EmptyDescription> : null}</EmptyHeader> : <span className="sr-only">Select a member to view their profile.</span>}
      {children}
    </Empty>
  );
}
