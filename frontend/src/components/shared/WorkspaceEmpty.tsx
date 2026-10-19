import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";

export default function WorkspaceEmpty({ title, description }: { title?: string; description?: string }) {
  return (
    <Empty className="min-h-0 gap-4 rounded-none border-0 p-6">
      {title ? <EmptyHeader><EmptyTitle>{title}</EmptyTitle>{description ? <EmptyDescription>{description}</EmptyDescription> : null}</EmptyHeader> : <span className="sr-only">Select a member to view their profile.</span>}
    </Empty>
  );
}
