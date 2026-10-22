import { useEffect, useState } from "react";
import { api } from "@/api";
import type { ComputerState } from "@/types";

export function useComputerState(taskId: string, working: boolean, enabled = true, computerKey = taskId) {
  const [computer, setComputer] = useState<ComputerState | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    setComputer(null);
    setError("");
  }, [computerKey]);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await api<ComputerState>("GET", `/api/tasks/${taskId}/computer`);
        if (!stopped) { setComputer(next); setError(""); }
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, working ? 1500 : 4000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [taskId, working, enabled]);

  return { computer, error };
}
