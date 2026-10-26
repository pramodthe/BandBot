import { api } from "@/api";
import type { ChannelInvitation, PeopleResponse } from "@/types";
import { Button } from "@/components/ui/button";
import PeopleSection, { type RunAction } from "./PeopleSection";

const rowClass = "flex flex-wrap items-center gap-2 rounded-xl border p-3";
const handleOf = (handle: string) => handle.replace(/^@/, "");

/** Contact requests and channel invitations, received and sent. */
export default function PendingRequests({ people, invitations, busy, run, notify }: {
  people: PeopleResponse;
  invitations: ChannelInvitation[];
  busy: boolean;
  run: RunAction;
  notify: (message: string) => void;
}) {
  return <>
    {people.requests.length ? (
      <PeopleSection title="Contact requests">
        {people.requests.map((request) => (
          <div key={request.id} className={rowClass}>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">{request.name}</p>
              <p className="text-muted-foreground text-xs">@{handleOf(request.handle)}</p>
              {request.message ? <p className="mt-1 text-sm">{request.message}</p> : null}
            </div>
            {[true, false].map((accept) => (
              <Button
                key={String(accept)}
                variant={accept ? "default" : "ghost"}
                size="sm"
                disabled={busy}
                onClick={() => void run(async () => { await api("POST", `/api/people/requests/${request.id}/decision`, { accept }); })}
              >
                {accept ? "Accept" : "Decline"}
              </Button>
            ))}
          </div>
        ))}
      </PeopleSection>
    ) : null}
    {invitations.length ? (
      <PeopleSection title="Channel invitations">
        {invitations.map((invitation) => (
          <div key={invitation.id} className={rowClass}>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">#{invitation.channel_name}</p>
              <p className="text-muted-foreground text-xs">Invited by {invitation.inviter_name}</p>
            </div>
            {[true, false].map((accept) => (
              <Button
                key={String(accept)}
                variant={accept ? "default" : "ghost"}
                size="sm"
                disabled={busy}
                onClick={() => void run(async () => {
                  await api("POST", `/api/people/invitations/${invitation.id}/decision`, { accept });
                  notify(accept ? "Channel joined. It is now in your conversations." : "Invitation declined.");
                })}
              >
                {accept ? "Join channel" : "Decline"}
              </Button>
            ))}
          </div>
        ))}
      </PeopleSection>
    ) : null}
    {people.sent_requests.length ? (
      <PeopleSection title="Sent requests">
        {people.sent_requests.map((request) => (
          <div key={request.id} className={rowClass}>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">{request.name}</p>
              <p className="text-muted-foreground text-xs">Waiting for @{handleOf(request.handle)}</p>
            </div>
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => void run(async () => {
                await api("POST", `/api/people/requests/${request.id}/cancel`);
                notify("Contact request cancelled.");
              })}
            >
              Cancel
            </Button>
          </div>
        ))}
      </PeopleSection>
    ) : null}
    {people.sent_invitations?.length ? (
      <PeopleSection title="Sent invitations">
        {people.sent_invitations.map((invitation) => (
          <div key={invitation.id} className={rowClass}>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">#{invitation.channel_name}</p>
              <p className="text-muted-foreground text-xs">Waiting for {invitation.recipient_name}</p>
            </div>
            <Button
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => void run(async () => {
                await api("POST", `/api/people/invitations/${invitation.id}/withdraw`);
                notify("Invitation withdrawn.");
              })}
            >
              Withdraw
            </Button>
          </div>
        ))}
      </PeopleSection>
    ) : null}
  </>;
}
