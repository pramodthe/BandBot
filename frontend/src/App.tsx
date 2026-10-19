import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, getToken, setToken, socketUrl } from "./api";
import AuthScreen from "@/components/workspace/AuthScreen";
import Workspace from "@/components/workspace/Workspace";
import { initialState, reducer, type State } from "./store";
import type { Bootstrap, ServerEvent } from "./types";

function Loaded({ boot, onSignOut }: { boot: Bootstrap; onSignOut: () => void }) {
  const [state, dispatch] = useReducer(reducer, boot, initialState);
  const [connection, setConnection] = useState(0);
  const requested = useRef(new Set<number>());

  useEffect(() => {
    const token = getToken();
    if (!token) return;
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      ws = new WebSocket(socketUrl(token));
      ws.onopen = () => {
        requested.current.clear();
        setConnection((n) => n + 1);
      };
      ws.onmessage = (e) => dispatch({ type: "event", event: JSON.parse(e.data) as ServerEvent });
      ws.onclose = (e) => {
        if (e.code === 4401) return onSignOut();
        if (!closed) retry = window.setTimeout(connect, 2000);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [onSignOut]);

  const activeId = state.activeChannelId;
  useEffect(() => {
    if (activeId == null || requested.current.has(activeId)) return;
    requested.current.add(activeId);
    api<State["messages"][number]>("GET", `/api/channels/${activeId}/messages`)
      .then((messages) => dispatch({ type: "messages_loaded", channelId: activeId, messages }))
      .catch(() => requested.current.delete(activeId));
  }, [activeId, connection]);

  return <Workspace state={state} dispatch={dispatch} onSignOut={onSignOut} />;
}

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [needsAuth, setNeedsAuth] = useState(!getToken());

  const load = useCallback(() => {
    api<Bootstrap>("GET", "/api/bootstrap")
      .then(setBoot)
      .catch(() => {
        setToken(null);
        setNeedsAuth(true);
      });
  }, []);

  useEffect(() => {
    if (!needsAuth) load();
  }, [needsAuth, load]);

  const signOut = useCallback(() => {
    setToken(null);
    setBoot(null);
    setNeedsAuth(true);
  }, []);

  if (needsAuth) {
    return (
      <AuthScreen
        onAuthed={(token) => {
          setToken(token);
          setNeedsAuth(false);
        }}
      />
    );
  }
  if (!boot) return <div className="text-muted-foreground grid h-full place-items-center">Loading workspace…</div>;
  return <Loaded key={boot.me.id} boot={boot} onSignOut={signOut} />;
}
