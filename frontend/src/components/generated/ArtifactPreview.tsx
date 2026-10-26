import { useMemo, useState } from "react";
import { DownloadIcon, MonitorIcon, SmartphoneIcon } from "lucide-react";
import type { Artifact } from "@/types";
import { WebPreview } from "@/components/assistant-ui/elements/web-preview";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import { ARTIFACT_SANDBOX, artifactDocument, artifactName, downloadArtifact } from "./artifacts";
import LiveAppPreviews, { type AppPreviewComponent } from "./LiveAppPreviews";

function PreviewFrame({ artifact, expanded = false, onExpand }: { artifact: Artifact; expanded?: boolean; onExpand?: () => void }) {
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [device, setDevice] = useState("desktop");
  const document = useMemo(() => artifactDocument(artifact.content), [artifact.content]);
  return <div className="flex min-h-0 flex-1 flex-col gap-3">
    {expanded ? <ToggleGroup type="single" value={device} onValueChange={(value) => value && setDevice(value)} aria-label="Preview screen size" variant="outline" size="sm">
      <ToggleGroupItem value="desktop" aria-label="Desktop preview"><MonitorIcon />Desktop</ToggleGroupItem>
      <ToggleGroupItem value="phone" aria-label="Phone preview"><SmartphoneIcon />Phone</ToggleGroupItem>
    </ToggleGroup> : null}
    <WebPreview origin={artifactName(artifact.path)} loading={loading} onReload={() => { setLoading(true); setRevision((value) => value + 1); }}
      onOpenExternal={onExpand} openLabel={expanded ? "Expanded app preview" : "Expand app preview"} className="min-h-0 max-w-none flex-1">
      <div className={cn("flex justify-center overflow-hidden bg-muted", expanded ? "h-full" : "h-[360px] max-sm:h-[300px]")}>
        <iframe key={`${artifact.path}-${revision}`} title={`App preview: ${artifactName(artifact.path)}`} srcDoc={document}
          sandbox={ARTIFACT_SANDBOX} referrerPolicy="no-referrer" onLoad={() => setLoading(false)}
          className={cn("h-full w-full border-0 bg-white transition-[max-width] motion-reduce:transition-none", device === "phone" && "max-w-[390px]")} />
      </div>
    </WebPreview>
  </div>;
}

export default function ArtifactPreview({ artifact, liveApps = [] }: { artifact: Artifact; liveApps?: AppPreviewComponent[] }) {
  const [expanded, setExpanded] = useState(false);
  return <>
    {liveApps.length > 0 ? <Tabs defaultValue="preview" className="min-w-0 gap-3">
      <TabsList aria-label="Webpage preview mode"><TabsTrigger value="preview">Preview</TabsTrigger><TabsTrigger value="live">Live app</TabsTrigger></TabsList>
      <TabsContent value="preview"><PreviewFrame artifact={artifact} onExpand={() => setExpanded(true)} /></TabsContent>
      <TabsContent value="live"><LiveAppPreviews apps={liveApps} hasSavedPreview /></TabsContent>
    </Tabs> : <PreviewFrame artifact={artifact} onExpand={() => setExpanded(true)} />}
    <Dialog open={expanded} onOpenChange={setExpanded}>
      <DialogContent className="flex h-[90dvh] w-[calc(100%-1.5rem)] max-w-[1400px] flex-col gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-[1400px]">
        <DialogHeader className="shrink-0 px-5 py-4 pr-12">
          <DialogTitle className="truncate">{artifactName(artifact.path)}</DialogTitle>
          <DialogDescription>App created by your teammate</DialogDescription>
        </DialogHeader>
        <Tabs defaultValue="preview" className="min-h-0 flex-1 gap-3 px-4 pb-4">
          <div className="flex shrink-0 items-center justify-between gap-2">
            <TabsList aria-label="App artifact view"><TabsTrigger value="preview">Preview</TabsTrigger><TabsTrigger value="code">Code</TabsTrigger></TabsList>
            <Button variant="outline" size="sm" onClick={() => downloadArtifact(artifact)}><DownloadIcon data-icon="inline-start" />Download</Button>
          </div>
          <TabsContent value="preview" className="flex min-h-0 flex-1 flex-col"><PreviewFrame artifact={artifact} expanded /></TabsContent>
          <TabsContent value="code" className="min-h-0 flex-1 overflow-auto rounded-xl border bg-muted p-4"><pre className="font-mono text-xs leading-relaxed"><code>{artifact.content}</code></pre></TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  </>;
}
