import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, getToken, setToken, socketUrl } from "./api";
import AuthScreen from "@/components/workspace/AuthScreen";
import Workspace from "@/components/workspace/Workspace";
import { initialState, reducer, type CaughtUpMessages, type State } from "./store";
import type { Bootstrap, ChannelInvitation, Message, PeopleResponse, ServerEvent, ServiceStatus } from "./types";

function Loaded({ boot, onSignOut }: { boot: Bootstrap; onSignOut: () => void }) {
  const [state, dispatch] = useReducer(reducer, boot, initialState);
  const stateRef = useRef(state);
  stateRef.current = state;
  const requested = useRef(new Set<number>());
  const catchingUp = useRef(false);
  const log = useRef<{ seq: number; event: ServerEvent }[]>([]);
  const seq = useRef(0);
  const generation = useRef(0);

  const catchUp = useCallback(async () => {
    const run = ++generation.current;
    catchingUp.current = true;
    const startSeq = seq.current;
    const stale = () => run !== generation.current;
    // Events received while this run is current. Taken synchronously so a newer run cannot reorder them.
    const takeEvents = () => {
      const events = log.current.filter((item) => item.seq > startSeq).map((item) => item.event);
      log.current = [];
      catchingUp.current = false;
      return events;
    };
    try {
      const next = await api<Bootstrap>("GET", "/api/bootstrap");
      if (stale()) return;
      const snapshot = stateRef.current;
      const loaded = await Promise.all(next.channels.map(async (channel) => {
        const existing = snapshot.messages[channel.id];
        const seen = Math.max(
          snapshot.readThrough[channel.id] ?? 0,
          existing?.reduce((max, message) => Math.max(max, message.id), 0) ?? 0,
        );
        const active = channel.id === snapshot.activeChannelId;
        if (!active && seen === 0) return null;
        const path = active
          ? `/api/channels/${channel.id}/messages`
          : `/api/channels/${channel.id}/messages?after=${seen}`;
        try {
          const messages = await api<Message[]>("GET", path);
          return { channelId: channel.id, messages, active } satisfies CaughtUpMessages;
        } catch {
          return null;
        }
      }));
      if (stale()) return;
      dispatch({
        type: "reconcile",
        boot: next,
        events: takeEvents(),
        loaded: loaded.filter((item): item is CaughtUpMessages => item !== null),
      });
    } catch {
      if (stale()) return;
      for (const event of takeEvents()) dispatch({ type: "event", event });
    }
  }, []);

  useEffect(() => {
    const token = getToken();
    if (!token) return;
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      ws = new WebSocket(socketUrl(token));
      ws.onopen = () => { void catchUp(); };
      ws.onmessage = (e) => {
        const event = JSON.parse(e.data) as ServerEvent;
        if (event.type === "provider_settings_changed") { void catchUp(); return; }
        log.current.push({ seq: ++seq.current, event });
        if (!catchingUp.current) dispatch({ type: "event", event });
      };
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
  }, [onSignOut, catchUp]);

  const activeId = state.activeChannelId;
  useEffect(() => {
    if (activeId == null || requested.current.has(activeId)) return;
    requested.current.add(activeId);
    api<State["messages"][number]>("GET", `/api/channels/${activeId}/messages`)
      .then((messages) => dispatch({ type: "messages_loaded", channelId: activeId, messages }))
      .catch(() => requested.current.delete(activeId));
  }, [activeId]);

  useEffect(() => {
    if (state.directoryRevision === 0) return;
    let cancelled = false;
    Promise.all([
      api<PeopleResponse>("GET", "/api/people"),
      api<ChannelInvitation[]>("GET", "/api/people/invitations"),
    ]).then(([people, invitations]) => {
      if (!cancelled) dispatch({ type: "people_loaded", invitations, contactRequests: people.requests });
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [state.directoryRevision]);

  useEffect(() => {
    let stopped = false;
    const poll = () => {
      api<ServiceStatus>("GET", "/api/status")
        .then((status) => { if (!stopped) dispatch({ type: "status", status }); })
        .catch(() => {});
    };
    poll();
    const timer = window.setInterval(poll, 10000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, []);

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
