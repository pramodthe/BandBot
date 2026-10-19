import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

export const formClass = "flex flex-col gap-3.5";
export const actionsClass = "flex justify-end gap-2";

export default function ModalShell({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className={cn(
          "flex max-h-[calc(100vh-48px)] flex-col gap-0 rounded-[10px] border-0 p-0 shadow-[0_20px_60px_rgba(0,0,0,0.35)]",
          wide ? "sm:max-w-[680px]" : "sm:max-w-[480px]",
        )}
      >
        <DialogHeader className="px-6 pt-[18px] pb-2 pr-12">
          <DialogTitle className="text-[21px] leading-tight font-bold [overflow-wrap:anywhere]">{title}</DialogTitle>
        </DialogHeader>
        <div className="overflow-y-auto px-6 pt-2 pb-[22px]">{children}</div>
      </DialogContent>
    </Dialog>
  );
}
