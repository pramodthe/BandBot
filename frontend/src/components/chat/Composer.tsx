import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { PaperclipIcon, SendHorizontalIcon, XIcon } from "lucide-react";
import { api, uploadDocument } from "@/api";
import type { Action, State } from "@/store";
import type { Channel, WorkspaceDocument } from "@/types";
import { deliverMessage } from "./deliver";
import { AttachmentCard } from "@/components/documents/DocumentsPanel";
import Avatar from "@/components/shared/Avatar";
import { Button } from "@/components/ui/button";
import { Command, CommandGroup, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";

interface Candidate {
  key: string;
  name: string;
  kind: "agent" | "human";
  detail: string;
  role?: string;
  avatar?: string | null;
  inChannel: boolean;
}

export default function Composer({ state, channel, dispatch, suggestion }: { state: State; channel: Channel; dispatch: (action: Action) => void; suggestion?: { text: string; revision: number } }) {
  const [text, setText] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const [highlight, setHighlight] = useState(0);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [attachments, setAttachments] = useState<WorkspaceDocument[]>([]);
  const [uploading, setUploading] = useState(false);
  const files = useRef<HTMLInputElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const inflight = useRef(false);
  const master = Object.values(state.agents).find((a) => a.is_master && a.owner_id === state.me.id);
  const dmAgent = channel.dm_agent_id != null ? state.agents[channel.dm_agent_id] : null;
  const dmPerson = state.users[channel.dm_user_ids?.find((id) => id !== state.me.id) ?? -1];

  useEffect(() => {
    if (!suggestion) return;
    setText((current) => current.trim() ? `${current}\n\n${suggestion.text}` : suggestion.text);
    setQuery(null);
    input.current?.focus();
  }, [suggestion]);

  const candidates = useMemo<Candidate[]>(() => {
    if (query === null) return [];
    const q = query.toLowerCase();
    const agents: Candidate[] = Object.values(state.agents).map((a) => ({
      key: `a${a.id}`,
      name: a.name,
      kind: "agent",
      detail: a.role_label,
      role: a.role,
      avatar: a.avatar,
      inChannel: channel.agent_ids.includes(a.id),
    }));
    const humans: Candidate[] = Object.values(state.users)
      .filter((u) => u.id !== state.me.id)
      .map((u) => ({
        key: `h${u.id}`,
        name: u.display_name,
        kind: "human",
        detail: `@${u.username}`,
        inChannel: channel.human_ids.includes(u.id),
      }));
    return [...agents, ...humans]
      .filter((c) => c.name.toLowerCase().includes(q))
      .sort((a, b) => Number(b.inChannel) - Number(a.inChannel) || Number(a.kind !== "agent") - Number(b.kind !== "agent"))
      .slice(0, 8);
  }, [query, state.agents, state.users, state.me.id, channel]);
  const menuOpen = candidates.length > 0;
  const upload = async (selected: FileList | File[]) => {
    if (uploading || sending) return;
    const next = Array.from(selected);
    if (next.length + attachments.length > 10) return setError("Attach up to 10 files per message.");
    if (next.some((file) => file.size > 25 * 1024 * 1024)) return setError("Each file must be 25 MiB or smaller.");
    setUploading(true); setError("");
    try {
      for (const file of next) {
        const doc = await uploadDocument(channel.id, file);
        setAttachments((values) => [...values, doc]);
      }
    } catch (e) { setError((e as Error).message); }
    finally { setUploading(false); if (files.current) files.current.value = ""; }
  };
  const removeAttachment = (doc: WorkspaceDocument) => {
    setAttachments((values) => values.filter((d) => d.id !== doc.id));
    void api("DELETE", `/api/documents/${doc.id}`).catch((e: Error) => setError(e.message));
  };

  const updateQuery = (value: string, caret: number) => {
    if (channel.is_dm) return;
    const match = /(^|\s)@([^\s@]*)$/.exec(value.slice(0, caret));
    setQuery(match ? match[2] : null);
    setHighlight(0);
  };

  const pick = (c: Candidate) => {
    const el = input.current;
    const caret = el?.selectionStart ?? text.length;
    const before = text.slice(0, caret).replace(/@([^\s@]*)$/, `@${c.name} `);
    const next = before + text.slice(caret);
    setText(next);
    setQuery(null);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(before.length, before.length);
    });
  };

  const send = () => {
    const content = text.trim();
    if ((!content && attachments.length === 0) || inflight.current || uploading) return;
    const attachmentIds = attachments.map((doc) => doc.id);
    const clientId = crypto.randomUUID();
    inflight.current = true;
    setSending(true);
    setError("");
    setText("");
    setAttachments([]);
    setQuery(null);
    void deliverMessage(dispatch, channel.id, content, attachmentIds, clientId).finally(() => {
      inflight.current = false;
      setSending(false);
      input.current?.focus();
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (menuOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        return setHighlight((h) => (h + 1) % candidates.length);
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        return setHighlight((h) => (h - 1 + candidates.length) % candidates.length);
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        return pick(candidates[highlight]);
      }
      if (e.key === "Escape") return setQuery(null);
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="relative px-6 pb-6 max-sm:px-4 max-sm:pb-4" onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); if (e.dataTransfer.files.length) void upload(e.dataTransfer.files); }}>
      <input ref={files} type="file" multiple className="sr-only" tabIndex={-1} aria-label="Choose documents" accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.png,.jpg,.jpeg,.webp" onChange={(e) => { if (e.target.files) void upload(e.target.files); }} />
      {attachments.length > 0 && <div className="mb-2 flex flex-wrap gap-2">{attachments.map((doc) => <div key={doc.id} className="flex items-start gap-1"><AttachmentCard document={doc} /><Button variant="ghost" size="icon-sm" aria-label={`Remove attachment ${doc.name}`} disabled={sending} onClick={() => removeAttachment(doc)}><XIcon /></Button></div>)}</div>}
      {uploading && <p className="mb-2 text-xs text-muted-foreground" role="status">Uploading documents…</p>}
      <Popover open={menuOpen} onOpenChange={(open) => !open && setQuery(null)}>
        <PopoverAnchor asChild>
          <div className="border-input focus-within:border-ring focus-within:ring-ring/20 flex items-end gap-2 rounded-2xl border bg-background py-3 ps-3 pe-2.5 shadow-xs focus-within:ring-2">
            <Button variant="ghost" size="icon-sm" aria-label="Attach documents" disabled={uploading || sending} onClick={() => files.current?.click()}><PaperclipIcon /></Button>
            <Textarea
              ref={input}
              rows={1}
              value={text}
              role="combobox"
              aria-label="Message"
              aria-expanded={menuOpen}
              aria-autocomplete="list"
              className="max-h-[200px] min-h-8 flex-1 resize-none rounded-none border-0 bg-transparent p-0 py-1.5 text-sm shadow-none focus-visible:ring-0 md:text-sm dark:bg-transparent"
              placeholder={
                dmPerson ? `Message ${dmPerson.display_name}` : dmAgent
                  ? `Message ${dmAgent.name}`
                  : master
                    ? `Message #${channel.name}. Type @ to mention a teammate`
                    : `Message #${channel.name}. Type @ to mention an agent or teammate`
              }
              onChange={(e) => {
                setText(e.target.value);
                updateQuery(e.target.value, e.target.selectionStart);
              }}
              onClick={(e) => updateQuery(text, e.currentTarget.selectionStart)}
              onKeyDown={onKeyDown}
              autoFocus
            />
            <Button
              size="icon-sm"
              className="rounded-xl"
              onClick={send}
              disabled={(!text.trim() && attachments.length === 0) || sending || uploading}
              title="Send"
              aria-label="Send"
            >
              <SendHorizontalIcon />
            </Button>
          </div>
        </PopoverAnchor>
        <PopoverContent
          side="top"
          align="start"
          sideOffset={6}
          className="w-[360px] p-1.5 shadow-[0_8px_24px_rgba(0,0,0,0.15)]"
          onOpenAutoFocus={(e) => e.preventDefault()}
          onCloseAutoFocus={(e) => e.preventDefault()}
          onMouseDown={(e) => e.preventDefault()}
        >
          <Command
            label="Mention someone"
            shouldFilter={false}
            value={candidates[highlight]?.key ?? ""}
            onValueChange={(key) => {
              const index = candidates.findIndex((c) => c.key === key);
              if (index >= 0) setHighlight(index);
            }}
          >
            <CommandList>
              <CommandGroup>
              {candidates.map((c) => (
                <CommandItem
                  key={c.key}
                  value={c.key}
                  onSelect={() => pick(c)}
                  className="group data-[selected=true]:bg-accent cursor-pointer gap-2 rounded-lg px-3 py-2 text-sm data-[selected=true]:text-accent-foreground"
                >
                  <Avatar name={c.name} kind={c.kind} role={c.role} avatar={c.avatar} size={22} />
                  <span className="font-bold">{c.name}</span>
                  <span className="text-muted-foreground truncate">
                    {c.detail}
                  </span>
                  {!c.inChannel && (
                    <span className="text-muted-foreground ms-auto shrink-0 text-xs">
                      {c.kind === "agent" ? "will join" : "not in channel"}
                    </span>
                  )}
                </CommandItem>
              ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      <p className="text-muted-foreground mt-2 px-1 text-[11px] max-sm:hidden">Enter to send · Shift + Enter for a new line</p>
      {error && <div className="text-destructive mt-1 text-[12.5px]">{error}</div>}
    </div>
  );
}
