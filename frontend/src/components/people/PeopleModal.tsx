import { useEffect, useState, type FormEvent } from "react";
import { UserPlusIcon } from "lucide-react";
import { api } from "@/api";
import type { State } from "@/store";
import type { BandAccount, BandPlatformConnection, Channel, ChannelInvitation, PeopleResponse } from "@/types";
import ModalShell from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import BandConnections from "./BandConnections";
import ContactList from "./ContactList";
import PendingRequests from "./PendingRequests";
import type { RunAction } from "./PeopleSection";

export default function PeopleModal({ state, channelId, personId, onChannel, onClose }: {
  state: State;
  channelId?: number;
  personId?: number;
  onChannel: (channel: Channel) => void;
  onClose: () => void;
}) {
  const [platform, setPlatform] = useState<BandPlatformConnection | null>(null);
  const [account, setAccount] = useState<BandAccount | null>(null);
  const [people, setPeople] = useState<PeopleResponse>({ contacts: [], requests: [], sent_requests: [] });
  const [invitations, setInvitations] = useState<ChannelInvitation[]>([]);
  const [handle, setHandle] = useState(personId ? state.users[personId]?.username || "" : "");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const channel = channelId ? state.channels[channelId] : null;

  const contactIds = new Set(people.contacts.map((person) => person.user_id));
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
    setPeople(contacts);
    setInvitations(invites);
    setPlatform(connection);
    setAccount(management);
  };

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        await refresh();
      } catch (cause) {
        if (!stopped) setError((cause as Error).message);
      } finally {
        if (!stopped) {
          setLoading(false);
          timer = window.setTimeout(poll, 3000);
        }
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [state.directoryRevision]);

  const run: RunAction = async (action) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      await refresh();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const request = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await api("POST", "/api/people/requests", { handle: handle.trim() });
      setNotice("Contact request sent. They can accept it in People.");
      setHandle("");
    });
  };

  return (
    <ModalShell title={channel ? `Invite people to #${channel.name}` : "People"} onClose={onClose} wide>
      <div className="space-y-6 py-2">
        <p className="text-muted-foreground text-sm">
          Add people by their app username. Chat privately or invite them into a channel with your team and AI teammates.
        </p>
        {loading ? <p className="text-muted-foreground text-sm" role="status">Loading people…</p> : null}
        {error ? <p className="text-destructive text-sm" role="alert">{error}</p> : null}
        {notice ? <p className="text-sm" role="status">{notice}</p> : null}
        <form onSubmit={request} className="space-y-2">
          <Label htmlFor="contact-handle">Find a person by app username</Label>
          <div className="flex flex-wrap gap-2">
            <Input
              id="contact-handle"
              placeholder="e.g. bob"
              value={handle}
              onChange={(e) => setHandle(e.target.value)}
              required
              disabled={busy}
              className="min-w-40 flex-1"
            />
            <Button variant="outline" disabled={busy || !handle.trim()}><UserPlusIcon />Send request</Button>
          </div>
        </form>
        <PendingRequests people={people} invitations={invitations} busy={busy} run={run} notify={setNotice} />
        <ContactList
          people={people}
          directory={directory}
          channel={channel}
          busy={busy}
          run={run}
          notify={setNotice}
          onChannel={onChannel}
        />
        <BandConnections
          account={account}
          platform={platform}
          busy={busy}
          run={run}
          notify={setNotice}
          onAccount={setAccount}
          onPlatform={setPlatform}
        />
      </div>
    </ModalShell>
  );
}
