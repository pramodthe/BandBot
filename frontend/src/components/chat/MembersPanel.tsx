import { useState, type ReactNode } from "react";
import { XIcon } from "lucide-react";
import { api } from "@/api";
import { roleOf, type State } from "@/store";
import type { BandPeer, Channel } from "@/types";
import { cn } from "@/lib/utils";
import Avatar from "@/components/shared/Avatar";
import ChannelControls from "./ChannelControls";
import StatusDot from "@/components/shared/StatusDot";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { Modal } from "@/components/workspace/types";

const hint = "text-muted-foreground text-[12.5px]";
const memberName = "flex min-w-0 flex-1 flex-col items-start text-left text-sm font-bold";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5 border-b border-[#f0f0f0] px-4 pt-2 pb-3.5">
      <div className="mb-1 text-sm font-black">{title}</div>
      {children}
    </div>
  );
}

function MemberName({ name, sub }: { name: string; sub: string }) {
  return (
    <>
      <span className="max-w-full truncate">{name}</span>
      <span className={cn(hint, "font-normal")}>{sub}</span>
    </>
  );
}

function RemoveButton({ onClick }: { onClick: () => void }) {
  return (
    <Button
      variant="ghost"
      size="icon-xs"
      className="text-muted-foreground hover:text-foreground hover:bg-[#eee]"
      title="Remove from channel"
      aria-label="Remove from channel"
      onClick={onClick}
    >
      <XIcon />
    </Button>
  );
}

function Picker({ placeholder, onPick, children }: { placeholder: string; onPick: (value: string) => void; children: ReactNode }) {
  return (
    <Select value="" onValueChange={onPick}>
      <SelectTrigger size="sm" className="mt-1.5 w-full border-dashed border-[#bbb] bg-white shadow-none">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent position="popper">
        {children}
      </SelectContent>
    </Select>
  );
}

export default function MembersPanel({
  state,
  channel,
  openModal,
  onClose,
}: {
  state: State;
  channel: Channel;
  openModal: (m: Modal) => void;
  onClose: () => void;
}) {
  const [error, setError] = useState("");
  const [peers, setPeers] = useState<BandPeer[] | null>(null);
  const humans = channel.human_ids.map((id) => state.users[id]).filter(Boolean);
  const agents = channel.agent_ids.map((id) => state.agents[id]).filter(Boolean);
  const available = Object.values(state.agents).filter((a) => !channel.agent_ids.includes(a.id) && (a.owner_id == null || a.owner_id === state.me.id));
  const joined = channel.human_ids.includes(state.me.id);
  const canInviteOutside = joined && (!channel.private || channel.owner_id === state.me.id);
  const owner = roleOf(state, state.me.id) === "owner";

  const run = (p: Promise<unknown>) => p.then(() => setError("")).catch((e) => setError((e as Error).message));
  const loadPeers = () =>
    run(api<BandPeer[]>("GET", `/api/channels/${channel.id}/peers`).then(setPeers));
  const invite = (bandId: string) =>
    run(api("POST", `/api/channels/${channel.id}/outside-agents`, { band_id: bandId }).then(() => setPeers(null)));

  return (
    <aside className="border-border bg-background narrow:absolute narrow:inset-y-0 narrow:right-0 narrow:z-20 narrow:shadow-lg w-[280px] max-w-full shrink-0 overflow-y-auto border-l py-3">
      <div className="flex items-center justify-between px-4 pb-3"><h2 className="text-sm font-semibold">Channel members</h2><Button variant="ghost" size="icon-xs" aria-label="Close channel members" onClick={onClose}><XIcon /></Button></div>
      {channel.private && channel.owner_id === state.me.id ? <div className="px-4 pb-3"><Button variant="outline" className="w-full" onClick={() => openModal({ kind: "people", channelId: channel.id })}>Invite people</Button></div> : null}
      <Section title={`Agents in #${channel.name}`}>
        {agents.length === 0 && <div className={hint}>No agents yet.</div>}
        {agents.map((a) => (
          <div key={a.id} className="flex items-center gap-2">
            <Avatar name={a.name} kind="agent" role={a.role} avatar={a.avatar} runtime={a.runtime} size={28} />
            <button className={memberName} onClick={() => openModal({ kind: "agent", agentId: a.id })}>
              <MemberName name={a.name} sub={a.role_label} />
            </button>
            <StatusDot state={a.status.state} title={a.status.error ?? a.status.state} />
            {!a.is_master && (a.owner_id == null || a.owner_id === state.me.id || channel.owner_id === state.me.id) && <RemoveButton onClick={() => run(api("DELETE", `/api/channels/${channel.id}/agents/${a.id}`))} />}
          </div>
        ))}
        {available.length > 0 ? (
          <Picker
            placeholder="+ Add an agent to this channel…"
            onPick={(id) => run(api("POST", `/api/channels/${channel.id}/agents`, { agent_id: Number(id) }))}
          >
            {available.map((a) => (
              <SelectItem key={a.id} value={String(a.id)}>
                {a.name} ({a.role_label})
              </SelectItem>
            ))}
          </Picker>
        ) : (
          <Button variant="secondary" size="sm" className="self-start text-[13px]" onClick={() => openModal({ kind: "new-agent" })}>
            + Add agents
          </Button>
        )}
      </Section>

      <Section title="Outside Band agents">
        {channel.outside_agents.length === 0 && (
          <div className={hint}>Agents running elsewhere on Band (Claude Code, Codex, other apps).</div>
        )}
        {channel.outside_agents.map((o) => (
          <div key={o.band_id} className="flex items-center gap-2">
            <Avatar name={o.name} kind="external" size={28} />
            <span className={memberName} title={o.description}>
              <MemberName name={o.name} sub={`@${o.handle}`} />
            </span>
            {canInviteOutside && (
              <RemoveButton
                onClick={() => run(api("DELETE", `/api/channels/${channel.id}/outside-agents/${encodeURIComponent(o.band_id)}`))}
              />
            )}
          </div>
        ))}
        {!canInviteOutside ? (
          <div className={hint}>The channel owner can invite or remove outside Band agents.</div>
        ) : peers === null ? (
          <Button variant="link" size="inline" className="self-start text-[12.5px]" onClick={loadPeers} disabled={!channel.band_room_id}>
            + Invite a Band agent
          </Button>
        ) : peers.length > 0 ? (
          <Picker placeholder="Choose a Band agent…" onPick={invite}>
            {peers.map((p) => (
              <SelectItem key={p.band_id} value={p.band_id}>
                {p.name} (@{p.handle}){p.online ? "" : " · offline"}
              </SelectItem>
            ))}
          </Picker>
        ) : (
          <div className={hint}>No other Band agents are available to invite.</div>
        )}
      </Section>

      <Section title="People">
        {humans.map((u) => {
          const role = roleOf(state, u.id);
          return (
            <div key={u.id} className="flex items-center gap-2">
              <Avatar name={u.display_name} kind="human" size={28} />
              <span className={memberName}>
                <span className="flex max-w-full items-center gap-1.5">
                  <span className="truncate">{u.display_name}</span>
                  {role !== "member" && <Badge variant="tag">{role === "owner" ? "Owner" : "Admin"}</Badge>}
                </span>
                <span className={cn(hint, "font-normal")}>@{u.username}</span>
              </span>
              {owner && role !== "owner" && (
                <Button
                  variant="link"
                  size="inline"
                  className="text-[12px]"
                  onClick={() => run(api("PUT", `/api/users/${u.id}/role`, { role: role === "admin" ? "member" : "admin" }))}
                >
                  {role === "admin" ? "Remove admin" : "Make admin"}
                </Button>
              )}
              <StatusDot state={u.online ? "online" : "stopped"} />
            </div>
          );
        })}
        <Button
          variant="link"
          size="inline"
          className="self-start text-[12.5px]"
          onClick={() => run(api("POST", `/api/channels/${channel.id}/${joined ? "leave" : "join"}`))}
        >
          {joined ? "Leave channel" : "Join channel"}
        </Button>
      </Section>

      <Section title="Permissions and spending">
        <ChannelControls state={state} channel={channel} />
      </Section>

      <p className={cn(hint, "border-b border-[#f0f0f0] px-4 pt-2 pb-3.5 leading-relaxed")}>
        {channel.band_room_id ? (
          <>
            This channel is the Band room <code>#{channel.name}</code> (<code>{channel.band_room_id.slice(0, 8)}</code>). The
            whole conversation is also visible at{" "}
            <a className="text-mention hover:underline" href="https://app.band.ai" target="_blank" rel="noreferrer">
              app.band.ai
            </a>
          </>
        ) : (
          "Local channel: people and agents collaborate here without a native Band room."
        )}
      </p>
      {error && <div className="text-destructive px-4 text-[12.5px]">{error}</div>}
    </aside>
  );
}
