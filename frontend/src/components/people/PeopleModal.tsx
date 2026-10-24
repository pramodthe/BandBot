import { useEffect, useState, type FormEvent } from "react";
import { MessageCircleIcon, UserPlusIcon } from "lucide-react";
import { api } from "@/api";
import type { State } from "@/store";
import type { BandAccount, BandPlatformConnection, Channel, ChannelInvitation, PeopleResponse } from "@/types";
import ModalShell from "@/components/shared/ModalShell";
import Avatar from "@/components/shared/Avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function PeopleModal({ state, channelId, personId, onChannel, onClose }: {
  state: State;
  channelId?: number;
  personId?: number;
  onChannel: (channel: Channel) => void;
  onClose: () => void;
}) {
  const [platform, setPlatform] = useState<BandPlatformConnection | null>(null);
  const [account, setAccount] = useState<BandAccount | null>(null);
  const [accountKey, setAccountKey] = useState("");
  const [platformKey, setPlatformKey] = useState("");
  const [people, setPeople] = useState<PeopleResponse>({ contacts: [], requests: [], sent_requests: [] });
  const [invitations, setInvitations] = useState<ChannelInvitation[]>([]);
  const [handle, setHandle] = useState(personId ? state.users[personId]?.username || "" : "");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const channel = channelId ? state.channels[channelId] : null;

  const contactIds = new Set(people.contacts.map((person) => person.user_id));
  const sentIds = new Set(people.sent_requests.map((person) => person.user_id));
  const receivedIds = new Set(people.requests.map((person) => person.user_id));
  const search = handle.trim().replace(/^@/, "").toLowerCase();
  const directory = Object.values(state.users).filter((person) => person.id !== state.me.id && !contactIds.has(person.id)
    && (!search || `${person.username} ${person.display_name}`.toLowerCase().includes(search)));
  const refresh = async () => {
    const [contacts, invites, connection, management] = await Promise.all([
      api<PeopleResponse>("GET", "/api/people"),
      api<ChannelInvitation[]>("GET", "/api/people/invitations"),
      api<BandPlatformConnection>("GET", "/api/band/connection"),
      api<BandAccount>("GET", "/api/band/account"),
    ]);
    setPeople(contacts); setInvitations(invites); setPlatform(connection); setAccount(management);
  };
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try { await refresh(); }
      catch (cause) { if (!stopped) setError((cause as Error).message); }
      finally {
        if (!stopped) { setLoading(false); timer = window.setTimeout(poll, 3000); }
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [state.directoryRevision]);

  const run = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await action(); await refresh(); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  const request = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await api("POST", "/api/people/requests", { handle: handle.trim() });
      setNotice("Contact request sent. They can accept it in People."); setHandle("");
    });
  };

  return <ModalShell title={channel ? `Invite people to #${channel.name}` : "People"} onClose={onClose} wide>
    <div className="space-y-6 py-2">
      <p className="text-muted-foreground text-sm">Add people by their app username. Chat privately or invite them into a channel with your team and AI teammates.</p>
      {loading ? <p className="text-muted-foreground text-sm" role="status">Loading people…</p> : null}
      {error ? <p className="text-destructive text-sm" role="alert">{error}</p> : null}
      {notice ? <p className="text-sm" role="status">{notice}</p> : null}
      <form onSubmit={request} className="space-y-2">
        <Label htmlFor="contact-handle">Find a person by app username</Label>
        <div className="flex flex-wrap gap-2"><Input id="contact-handle" placeholder="e.g. bob" value={handle} onChange={(e) => setHandle(e.target.value)} required disabled={busy} className="min-w-40 flex-1" /><Button variant="outline" disabled={busy || !handle.trim()}><UserPlusIcon />Send request</Button></div>
      </form>
      {people.requests.length ? <section className="space-y-3"><h2 className="text-sm font-semibold">Contact requests</h2>{people.requests.map((request) => <div key={request.id} className="flex flex-wrap items-center gap-2 rounded-xl border p-3">
        <div className="min-w-0 flex-1"><p className="text-sm font-medium">{request.name}</p><p className="text-muted-foreground text-xs">@{request.handle.replace(/^@/, "")}</p>{request.message ? <p className="mt-1 text-sm">{request.message}</p> : null}</div>
        {[true, false].map((accept) => <Button key={String(accept)} variant={accept ? "default" : "ghost"} size="sm" disabled={busy} onClick={() => void run(async () => { await api("POST", `/api/people/requests/${request.id}/decision`, { accept }); })}>{accept ? "Accept" : "Decline"}</Button>)}
      </div>)}</section> : null}
      {invitations.length ? <section className="space-y-3"><h2 className="text-sm font-semibold">Channel invitations</h2>{invitations.map((invitation) => <div key={invitation.id} className="flex flex-wrap items-center gap-2 rounded-xl border p-3">
        <div className="min-w-0 flex-1"><p className="text-sm font-medium">#{invitation.channel_name}</p><p className="text-muted-foreground text-xs">Invited by {invitation.inviter_name}</p></div>
        {[true, false].map((accept) => <Button key={String(accept)} variant={accept ? "default" : "ghost"} size="sm" disabled={busy} onClick={() => void run(async () => { await api("POST", `/api/people/invitations/${invitation.id}/decision`, { accept }); setNotice(accept ? "Channel joined. It is now in your conversations." : "Invitation declined."); })}>{accept ? "Join channel" : "Decline"}</Button>)}
      </div>)}</section> : null}
      {people.sent_requests.length ? <section className="space-y-3"><h2 className="text-sm font-semibold">Sent requests</h2>{people.sent_requests.map((request) => <div key={request.id} className="flex flex-wrap items-center gap-2 rounded-xl border p-3">
        <div className="min-w-0 flex-1"><p className="text-sm font-medium">{request.name}</p><p className="text-muted-foreground text-xs">Waiting for @{request.handle.replace(/^@/, "")}</p></div>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(async () => { await api("POST", `/api/people/requests/${request.id}/cancel`); setNotice("Contact request cancelled."); })}>Cancel</Button>
      </div>)}</section> : null}
      {people.sent_invitations?.length ? <section className="space-y-3"><h2 className="text-sm font-semibold">Sent invitations</h2>{people.sent_invitations.map((invitation) => <div key={invitation.id} className="flex flex-wrap items-center gap-2 rounded-xl border p-3">
        <div className="min-w-0 flex-1"><p className="text-sm font-medium">#{invitation.channel_name}</p><p className="text-muted-foreground text-xs">Waiting for {invitation.recipient_name}</p></div>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => void run(async () => { await api("POST", `/api/people/invitations/${invitation.id}/withdraw`); setNotice("Invitation withdrawn."); })}>Withdraw</Button>
      </div>)}</section> : null}
      <section className="space-y-3"><h2 className="text-sm font-semibold">Contacts</h2>
        <p className="text-muted-foreground text-xs">Removing a contact keeps shared channels and message history. A new direct message needs a new accepted request. Blocking stops new direct messages and contact requests. Shared channels and their history stay as they are.</p>
        {!people.contacts.length ? <p className="text-muted-foreground text-sm">Send a contact request to get started.</p> : null}
        {people.contacts.map((person) => <div key={person.user_id} className="flex flex-wrap items-center gap-3 rounded-xl border p-3">
          <Avatar name={person.name} kind="human" size={32} /><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{person.name}</p><p className="text-muted-foreground truncate text-xs">@{person.handle.replace(/^@/, "")}</p></div>
          <Button size="sm" variant="outline" disabled={busy || !!channel?.human_ids.includes(person.user_id)} onClick={() => void run(async () => {
            if (channel) { await api("POST", `/api/channels/${channel.id}/invitations`, { user_id: person.user_id }); setNotice(`Invitation sent to ${person.name}.`); }
            else { const dm = await api<Channel>("POST", `/api/people/${person.user_id}/dm`); onChannel(dm); }
          })}>{channel ? <UserPlusIcon /> : <MessageCircleIcon />}{channel ? channel.human_ids.includes(person.user_id) ? "Already joined" : "Invite" : "Message"}</Button>
          {!channel && <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run(async () => { await api("DELETE", `/api/people/${person.user_id}`); setNotice(`${person.name} was removed. Shared channels and history stay.`); })}>Remove</Button>}
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run(async () => { await api("POST", `/api/people/${person.user_id}/block`); setNotice(`${person.name} is blocked. Shared channels stay open.`); })}>Block</Button>
        </div>)}
      </section>
      {people.blocked?.length ? <section className="space-y-3"><h2 className="text-sm font-semibold">Blocked</h2>{people.blocked.map((person) => <div key={person.user_id} className="flex flex-wrap items-center gap-3 rounded-xl border p-3">
        <Avatar name={person.name} kind="human" size={32} /><div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{person.name}</p><p className="text-muted-foreground truncate text-xs">@{person.handle.replace(/^@/, "")}</p></div>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(async () => { await api("POST", `/api/people/${person.user_id}/unblock`); setNotice(`${person.name} is unblocked.`); })}>Unblock</Button>
      </div>)}</section> : null}
      {directory.length ? <section className="space-y-3"><h2 className="text-sm font-semibold">People in this workspace</h2>{directory.map((person) => <div key={person.id} className="flex flex-wrap items-center gap-3 rounded-xl border p-3">
        <Avatar name={person.display_name} kind="human" size={32} /><div className="min-w-0 flex-1"><p className="text-sm font-medium">{person.display_name}</p><p className="text-muted-foreground text-xs">@{person.username}</p></div>
        <Button size="sm" variant="outline" disabled={busy || sentIds.has(person.id) || receivedIds.has(person.id)} onClick={() => void run(async () => {
          await api("POST", "/api/people/requests", { handle: person.username }); setNotice(`Contact request sent to ${person.display_name}.`);
        })}><UserPlusIcon />{sentIds.has(person.id) ? "Request sent" : receivedIds.has(person.id) ? "Request received" : `Add ${person.display_name}`}</Button>
      </div>)}</section> : null}
      <section aria-label="Your Band agent management" className="space-y-3 rounded-xl border p-4">
        <h2 className="text-sm font-semibold">Create and manage Band agents</h2>
        <p className="text-muted-foreground text-sm">{account?.connected
          ? `Connected as ${account.name || account.handle}. New agents are created in your Band account.`
          : "Connect your own Band user API key to create agents from this app. Other users use their own keys."}</p>
        <p className="text-muted-foreground text-xs">Your user account, people, channels, and human messages stay in BandBot.</p>
        <form className="space-y-2" onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            const saved = await api<BandAccount>("PUT", "/api/band/account", { api_key: accountKey.trim() });
            setAccount(saved); setAccountKey(""); setNotice("Your Band account is connected. You can now create agents.");
          });
        }}>
          <Label htmlFor="management-band-key">Band user API key</Label>
          <div className="flex flex-wrap gap-2"><Input id="management-band-key" type="password" autoComplete="off" placeholder="band_u_…" required disabled={busy}
            value={accountKey} onChange={(event) => setAccountKey(event.target.value)} className="min-w-40 flex-1" />
            <Button disabled={busy || !accountKey.trim()}>{account?.connected ? "Update management key" : "Connect for agent management"}</Button></div>
          <p className="text-muted-foreground text-xs">Create a user key in <a href="https://app.band.ai/users/settings" target="_blank" rel="noreferrer" className="underline">Band Settings → API Keys</a>. It is verified and stored encrypted.</p>
        </form>
      </section>
      <section aria-label="Your Band platform connection" className="space-y-3 rounded-xl border p-4">
        <h2 className="text-sm font-semibold">Existing agent connection</h2>
        <p className="text-muted-foreground text-sm">{platform?.connected
          ? platform.online === false ? platform.error || "Your saved Band connection is unavailable. Update your key to reconnect." : "Your Band connection is active. Rooms, teammates and Band collaboration use your saved key."
          : "Optional: connect an existing agent identity with its band_a_ key. This key cannot create new native Band identities."}</p>
        <form className="space-y-2" onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            const connection = await api<BandPlatformConnection>("PUT", "/api/band/connection", { api_key: platformKey.trim() });
            setPlatform(connection); setPlatformKey(""); setNotice("Your Band connection has been verified and saved.");
          });
        }}>
          <Label htmlFor="platform-band-key">{platform?.connected ? "Update Band Agent API key" : "Band Agent API key"}</Label>
          <div className="flex flex-wrap gap-2"><Input id="platform-band-key" type="password" autoComplete="off" placeholder="band_a_…" required disabled={busy}
            value={platformKey} onChange={(event) => setPlatformKey(event.target.value)} className="min-w-40 flex-1" />
            <Button disabled={busy || !platformKey.trim()}>{platform?.connected ? "Update connection" : "Connect Band"}</Button></div>
        </form>
      </section>
    </div>
  </ModalShell>;
}
