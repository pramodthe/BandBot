import { useState } from "react";
import { ShieldAlertIcon } from "lucide-react";
import { api } from "@/api";
import type { MessageApproval } from "@/types";
import { ApprovalCard } from "@/components/assistant-ui/elements/approval-card";

type Shown = MessageApproval["status"] | "sending";

function receipt(approval: MessageApproval, shown: Shown, approving: boolean) {
  const by = approval.decided_by ? ` by ${approval.decided_by}` : "";
  if (shown === "sending") return { state: "running" as const, label: approving ? "Approving…" : "Denying…" };
  if (shown === "approved") return { state: "done" as const, label: `Approved${by}` };
  if (shown === "denied") {
    return { state: "denied" as const, label: approval.note === "Task cancelled" ? `Task cancelled${by}` : `Denied${by}` };
  }
  return { state: "request" as const, label: undefined };
}

export default function ApprovalRequest({ approval, agentName, blocked }: {
  approval: MessageApproval; agentName: string; blocked: string | null;
}) {
  const [sending, setSending] = useState<boolean | null>(null);
  const [decided, setDecided] = useState<"approved" | "denied" | null>(null);
  const [error, setError] = useState("");

  const decide = async (approve: boolean) => {
    setSending(approve);
    setError("");
    try {
      await api("POST", `/api/approvals/${approval.id}`, { approve });
      setDecided(approve ? "approved" : "denied");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSending(null);
    }
  };

  // The server's copy wins once it arrives; until then, show what this person just chose.
  const shown: Shown = approval.status !== "pending" ? approval.status : sending !== null ? "sending" : decided ?? "pending";
  const { state, label } = receipt(approval, shown, sending ?? true);

  return (
    <div className="flex flex-col gap-1.5 pt-1.5">
      <ApprovalCard
        className="max-w-md"
        state={state}
        statusLabel={label}
        icon={<ShieldAlertIcon className="size-4" />}
        title={approval.action}
        subtitle={blocked ? `${agentName} needs an admin's approval` : `${agentName} needs your approval`}
        description={approval.details || undefined}
        allowOnceLabel="Approve"
        onAllowOnce={blocked ? undefined : () => void decide(true)}
        onDeny={blocked ? undefined : () => void decide(false)}
      />
      {blocked && shown === "pending" && <p className="text-muted-foreground text-xs">{blocked}</p>}
      {error && (
        <p role="alert" className="text-destructive text-xs">
          {error}
        </p>
      )}
    </div>
  );
}
