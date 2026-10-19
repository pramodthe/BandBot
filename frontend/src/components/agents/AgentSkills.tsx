import { useEffect, useRef, useState } from "react";
import { BookOpenIcon, CheckIcon, CopyIcon, DownloadIcon, FileTextIcon, FolderIcon, LoaderCircleIcon, MoreHorizontalIcon, PauseIcon, PencilIcon, PlayIcon, PlusIcon, Trash2Icon, UploadIcon } from "lucide-react";
import { api, downloadSkill, uploadSkill } from "@/api";
import type { AgentSkillsResponse, SkillSummary } from "@/types";
import { cn } from "@/lib/utils";
import ModalShell from "@/components/shared/ModalShell";
import SkillEditor from "./SkillEditor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Editor = { skillId?: string; copy?: boolean } | null;

export default function AgentSkills({ agentId, canManage, revision }: {
  agentId: number;
  canManage: boolean;
  revision: number;
}) {
  const [data, setData] = useState<AgentSkillsResponse | null>(null);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [editor, setEditor] = useState<Editor>(null);
  const [query, setQuery] = useState("");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let stopped = false;
    api<AgentSkillsResponse>("GET", `/api/agents/${agentId}/skills`).then((value) => {
      if (!stopped) { setData(value); setError(""); }
    }).catch((cause) => { if (!stopped) setError((cause as Error).message); });
    return () => { stopped = true; };
  }, [agentId, revision, reload]);

  const run = async (operation: () => Promise<unknown>, message: string) => {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      setNotice(message);
      setReload((value) => value + 1);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const importFiles = (files: File[]) => files.length === 0 ? Promise.resolve() : run(async () => {
    const size = files.reduce((total, file) => total + file.size, 0);
    if (size > 2 * 1024 * 1024 || files.length > 40) throw new Error("Import up to 40 text files and at most 2 MiB.");
    if (files.length === 1 && !files[0].webkitRelativePath) {
      await uploadSkill(`/api/skills/import?agent_id=${agentId}&filename=${encodeURIComponent(files[0].name)}`, files[0]);
    } else {
      const bundle = await Promise.all(files.map(async (file) => {
        const bytes = await file.arrayBuffer();
        try {
          return { path: file.webkitRelativePath || file.name, content: new TextDecoder("utf-8", { fatal: true }).decode(bytes) };
        } catch {
          throw new Error(`${file.name} must be a UTF-8 text file.`);
        }
      }));
      await api("POST", `/api/skills/import?agent_id=${agentId}`, { files: bundle });
    }
    setAddOpen(false);
  }, "Skill imported and added to this agent.");

  const installed = data?.installed ?? [];
  const byId = new Map(installed.map((item) => [item.skill_id, item]));
  const library = data?.library ?? [];
  const installedSkills = library.filter((skill) => byId.has(skill.id));
  const available = library.filter((skill) => !byId.has(skill.id));
  const matches = (skill: SkillSummary) => `${skill.name} ${skill.description} ${skill.created_by_name}`.toLowerCase().includes(query.trim().toLowerCase());
  const edit = (skill: SkillSummary, copy = false) => { setAddOpen(false); setEditor({ skillId: skill.id, copy }); };
  const install = (skill: SkillSummary) => run(() => api("POST", `/api/agents/${agentId}/skills`, { skill_id: skill.id }), `${skill.name} added.`);
  const remove = (skill: SkillSummary) => run(() => api("DELETE", `/api/agents/${agentId}/skills/${skill.id}`), `${skill.name} removed from this agent. It remains in the library.`);
  const exportFile = (skill: SkillSummary) => run(() => downloadSkill(skill.id, skill.name), `${skill.name} exported.`);

  const feedback = <>
    {error ? <p role="alert" className="text-destructive text-sm">{error}</p> : null}
    {notice ? <p role="status" className="text-muted-foreground text-sm">{notice}</p> : null}
  </>;

  const skillRow = (skill: SkillSummary, inLibrary = false) => {
    const assignment = byId.get(skill.id);
    return (
      <div key={skill.id} className="flex items-start gap-3 px-4 py-4">
        <FileTextIcon className="mt-1 size-[18px] shrink-0" strokeWidth={1.5} aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <button className="break-all text-left text-sm font-medium hover:underline" onClick={() => edit(skill)}>{skill.name}</button>
            <span className="text-muted-foreground text-xs">Created by {skill.created_by_name}</span>
            {assignment && !assignment.enabled ? <Badge variant="secondary">Disabled</Badge> : null}
          </div>
          <p className="text-muted-foreground text-[13px] leading-relaxed break-words">{skill.description}</p>
        </div>
        {inLibrary ? (
          <Button variant="ghost" size="sm" disabled={busy || !canManage} aria-label={`${assignment ? "Remove" : "Add"} ${skill.name}`} onClick={() => assignment ? void remove(skill) : void install(skill)}>
            {assignment ? <CheckIcon /> : <PlusIcon />}{assignment ? "Remove" : "Add"}
          </Button>
        ) : (
          <DropdownMenu>
            <DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label={`Manage ${skill.name}`}><MoreHorizontalIcon /></Button></DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                <DropdownMenuItem onSelect={() => edit(skill)}><BookOpenIcon />View instructions</DropdownMenuItem>
                {skill.builtin ? <DropdownMenuItem disabled={!canManage || busy} onSelect={() => edit(skill, true)}><CopyIcon />Create a copy</DropdownMenuItem> : <DropdownMenuItem disabled={!canManage || busy} onSelect={() => edit(skill)}><PencilIcon />Edit skill</DropdownMenuItem>}
                <DropdownMenuItem disabled={busy} onSelect={() => { void exportFile(skill); }}><DownloadIcon />Export ZIP</DropdownMenuItem>
                <DropdownMenuItem disabled={!canManage || busy} onSelect={() => { void run(() => api("PATCH", `/api/agents/${agentId}/skills/${skill.id}`, { enabled: !assignment?.enabled }), `${skill.name} ${assignment?.enabled ? "disabled" : "enabled"}.`); }}>
                  {assignment?.enabled ? <PauseIcon /> : <PlayIcon />}{assignment?.enabled ? "Disable" : "Enable"}
                </DropdownMenuItem>
              </DropdownMenuGroup>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                <DropdownMenuItem disabled={!canManage || busy} onSelect={() => { void remove(skill); }}><Trash2Icon />Remove from agent</DropdownMenuItem>
                {!skill.builtin ? <DropdownMenuItem variant="destructive" disabled={!canManage || busy} onSelect={() => {
                  if (confirm(`Delete ${skill.name} from the library and every agent using it? Export it first if you need a copy.`)) {
                    void run(() => api("DELETE", `/api/skills/${skill.id}`), "Skill deleted from the library.");
                  }
                }}><Trash2Icon />Delete from library</DropdownMenuItem> : null}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    );
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm">Extend this agent’s capabilities with reusable skills.</p>
        <Button variant="ghost" size="sm" disabled={!canManage} onClick={() => { setAddOpen(true); setError(""); setNotice(""); }}><PlusIcon data-icon="inline-start" />Add skills</Button>
      </div>
      <p className="text-muted-foreground text-xs">Enabled skills guide new tasks. {canManage ? "Manage the instructions and workflows this agent can use." : "Only workspace admins can manage skills."}</p>
      {!addOpen ? feedback : null}
      {!data ? <div role="status" className="text-muted-foreground flex items-center gap-2 py-8 text-sm"><LoaderCircleIcon className="size-4 animate-spin motion-reduce:animate-none" />{error ? <Button variant="link" size="inline" onClick={() => setReload((value) => value + 1)}>Retry loading skills</Button> : "Loading skills…"}</div> : installedSkills.length ? (
        <div className="border-border divide-border divide-y overflow-hidden rounded-xl border">{installedSkills.map((skill) => skillRow(skill))}</div>
      ) : (
        <Empty className="border-border rounded-xl border py-12">
          <EmptyHeader><EmptyMedia variant="icon"><BookOpenIcon /></EmptyMedia><EmptyTitle>No skills yet</EmptyTitle><EmptyDescription>Add a skill from the library, create one, or import your own workflow.</EmptyDescription></EmptyHeader>
          <Button variant="outline" disabled={!canManage} onClick={() => setAddOpen(true)}><PlusIcon data-icon="inline-start" />Add skills</Button>
        </Empty>
      )}
      {addOpen ? <ModalShell title="Add skills" wide onClose={() => !busy && setAddOpen(false)}>
        <div className="flex flex-col gap-4">
          <div aria-label="Import local skills" className={cn("border-border flex flex-col gap-3 rounded-xl border border-dashed p-4", dragging && "bg-accent")}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)}
            onDrop={(event) => { event.preventDefault(); setDragging(false); if (!busy) void importFiles(Array.from(event.dataTransfer.files)); }}>
            <div className="flex items-start gap-3"><UploadIcon className="text-muted-foreground mt-0.5 size-5 shrink-0" /><div><h3 className="text-sm font-medium">Import local skills</h3><p className="text-muted-foreground mt-1 text-xs">Drop a SKILL.md or ZIP here, or choose a skill folder. Supporting files must be UTF-8 text.</p></div></div>
            <div className="flex flex-wrap gap-2">
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => folderInput.current?.click()}><FolderIcon data-icon="inline-start" />Folder</Button>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => fileInput.current?.click()}><UploadIcon data-icon="inline-start" />ZIP/File</Button>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => { setAddOpen(false); setEditor({}); }}><PlusIcon data-icon="inline-start" />Create skill</Button>
            </div>
            <input ref={fileInput} type="file" accept=".md,.zip" className="hidden" aria-label="Import skill file" onChange={(event) => { const files = Array.from(event.target.files ?? []); event.target.value = ""; if (files.length) void importFiles(files); }} />
            <input ref={folderInput} type="file" multiple {...{ webkitdirectory: "" }} className="hidden" aria-label="Import skill folder" onChange={(event) => { const files = Array.from(event.target.files ?? []); event.target.value = ""; if (files.length) void importFiles(files); }} />
          </div>
          {feedback}
          {busy ? <p role="status" className="text-muted-foreground text-sm">Saving skill changes…</p> : null}
          <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search by name, creator, or description" aria-label="Search skills" />
          <Tabs defaultValue="available">
            <TabsList className="w-full"><TabsTrigger value="available">Available ({available.length})</TabsTrigger><TabsTrigger value="installed">Installed ({installed.length})</TabsTrigger></TabsList>
            <TabsContent value="available" className="max-h-80 overflow-y-auto">
              {available.filter(matches).map((skill) => skillRow(skill, true))}
              {available.filter(matches).length === 0 ? <p className="text-muted-foreground px-4 py-8 text-center text-sm">No matching skills available.</p> : null}
            </TabsContent>
            <TabsContent value="installed" className="max-h-80 overflow-y-auto">
              {installedSkills.filter(matches).map((skill) => skillRow(skill, true))}
              {installedSkills.filter(matches).length === 0 ? <p className="text-muted-foreground px-4 py-8 text-center text-sm">No matching installed skills.</p> : null}
            </TabsContent>
          </Tabs>
        </div>
      </ModalShell> : null}
      {editor ? <SkillEditor key={`${editor.skillId ?? "new"}-${editor.copy ?? false}`} {...editor} agentId={agentId} canManage={canManage} onClose={() => setEditor(null)} onSaved={(message) => { setEditor(null); setNotice(message); setReload((value) => value + 1); }} /> : null}
    </div>
  );
}
