import { useEffect, useState } from "react";
import { api } from "@/api";
import type { Agent, ImageGenerationActivity as Activity } from "@/types";
import { ImageGeneration } from "@/components/assistant-ui/elements/image-generation";
import Avatar from "@/components/shared/Avatar";
import GeneratedImage from "./GeneratedImage";

export function useImageGenerations(channelId: number) {
  const [state, setState] = useState<{ channelId: number; rows: Activity[] }>({ channelId, rows: [] });
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const rows = await api<Activity[]>("GET", `/api/channels/${channelId}/image-generations`);
        if (!stopped) setState({ channelId, rows });
      } catch {
        // A lost connection must not leave a stale generation animation running.
        if (!stopped) setState({ channelId, rows: [] });
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 1500);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channelId]);
  return state.channelId === channelId ? state.rows : [];
}

export default function ImageGenerationActivity({ activity, agent }: { activity: Activity; agent: Agent }) {
  return <div className="mx-auto flex w-full max-w-[920px] gap-3 px-6 py-3 max-sm:px-4" aria-label={`${agent.name}'s image generation`}>
    <Avatar name={agent.name} kind="agent" role={agent.role} avatar={agent.avatar} />
    <div className="min-w-0 flex-1">
      <p className="mb-2 text-sm font-semibold">{agent.name}</p>
      {activity.status === "generating" ? <ImageGeneration prompt={activity.title} generating size={activity.size}
        className="w-80 max-w-full" role="status" aria-label={`Generating ${activity.title}`} />
        : activity.status === "ready" && activity.document_id ? <GeneratedImage documentId={activity.document_id} title={activity.title} model={activity.model} />
          : <p role="alert" className="text-sm text-muted-foreground">{activity.error}</p>}
    </div>
  </div>;
}
