import { MessageCircleIcon, UserPlusIcon } from "lucide-react";
import { api } from "@/api";
import type { Channel, PeopleResponse, User } from "@/types";
import Avatar from "@/components/shared/Avatar";
import { Button } from "@/components/ui/button";
import PeopleSection, { type RunAction } from "./PeopleSection";

const rowClass = "flex flex-wrap items-center gap-3 rounded-xl border p-3";
const handleOf = (handle: string) => handle.replace(/^@/, "");

/** Accepted contacts, blocked people and everyone else in the workspace. */
export default function ContactList({ people, directory, channel, busy, run, notify, onChannel }: {
  people: PeopleResponse;
  /** Workspace members who aren't contacts yet, filtered by the search. */
  directory: User[];
  /** When inviting into a channel, contact actions invite instead of opening a DM. */
  channel: Channel | null;
  busy: boolean;
  run: RunAction;
  notify: (message: string) => void;
  onChannel: (channel: Channel) => void;
}) {
  const sentIds = new Set(people.sent_requests.map((person) => person.user_id));
  const receivedIds = new Set(people.requests.map((person) => person.user_id));
  return <>
    <PeopleSection title="Contacts">
      <p className="text-muted-foreground text-xs">
        Removing a contact keeps shared channels and message history. A new direct message needs a new accepted request.
        Blocking stops new direct messages and contact requests. Shared channels and their history stay as they are.
      </p>
      {!people.contacts.length ? <p className="text-muted-foreground text-sm">Send a contact request to get started.</p> : null}
      {people.contacts.map((person) => {
        const joined = !!channel?.human_ids.includes(person.user_id);
        return (
          <div key={person.user_id} className={rowClass}>
            <Avatar name={person.name} kind="human" size={32} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{person.name}</p>
              <p className="text-muted-foreground truncate text-xs">@{handleOf(person.handle)}</p>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || joined}
              onClick={() => void run(async () => {
                if (channel) {
                  await api("POST", `/api/channels/${channel.id}/invitations`, { user_id: person.user_id });
                  notify(`Invitation sent to ${person.name}.`);
                } else {
                  onChannel(await api<Channel>("POST", `/api/people/${person.user_id}/dm`));
                }
              })}
            >
              {channel ? <UserPlusIcon /> : <MessageCircleIcon />}
              {channel ? joined ? "Already joined" : "Invite" : "Message"}
            </Button>
            {!channel && (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => void run(async () => {
                  await api("DELETE", `/api/people/${person.user_id}`);
                  notify(`${person.name} was removed. Shared channels and history stay.`);
                })}
              >
                Remove
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => void run(async () => {
                await api("POST", `/api/people/${person.user_id}/block`);
                notify(`${person.name} is blocked. Shared channels stay open.`);
              })}
            >
              Block
            </Button>
          </div>
        );
      })}
    </PeopleSection>
    {people.blocked?.length ? (
      <PeopleSection title="Blocked">
        {people.blocked.map((person) => (
          <div key={person.user_id} className={rowClass}>
            <Avatar name={person.name} kind="human" size={32} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">{person.name}</p>
              <p className="text-muted-foreground truncate text-xs">@{handleOf(person.handle)}</p>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => void run(async () => {
                await api("POST", `/api/people/${person.user_id}/unblock`);
                notify(`${person.name} is unblocked.`);
              })}
            >
              Unblock
            </Button>
          </div>
        ))}
      </PeopleSection>
    ) : null}
    {directory.length ? (
      <PeopleSection title="People in this workspace">
        {directory.map((person) => (
          <div key={person.id} className={rowClass}>
            <Avatar name={person.display_name} kind="human" size={32} />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">{person.display_name}</p>
              <p className="text-muted-foreground text-xs">@{person.username}</p>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || sentIds.has(person.id) || receivedIds.has(person.id)}
              onClick={() => void run(async () => {
                await api("POST", "/api/people/requests", { handle: person.username });
                notify(`Contact request sent to ${person.display_name}.`);
              })}
            >
              <UserPlusIcon />
              {sentIds.has(person.id) ? "Request sent" : receivedIds.has(person.id) ? "Request received" : `Add ${person.display_name}`}
            </Button>
          </div>
        ))}
      </PeopleSection>
    ) : null}
  </>;
}
