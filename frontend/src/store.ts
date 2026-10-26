import type {
  Agent,
  BandStatus,
  Bootstrap,
  Channel,
  ChannelInvitation,
  ChannelPolicy,
  Draft,
  Message,
  Model,
  OutgoingMessage,
  PeopleResponse,
  Role,
  ServerEvent,
  ServiceStatus,
  TaskActivity,
  User,
  WorkspaceRole,
} from "./types";

export interface State {
  me: User;
  users: Record<number, User>;
  channels: Record<number, Channel>;
  agents: Record<number, Agent>;
  messages: Record<number, Message[]>;
  activity: Record<number, Record<string, TaskActivity>>;
  drafts: Record<number, Record<string, Draft>>;
  unread: Record<number, number>;
  /** Highest message id already counted or seen in a channel. */
  readThrough: Record<number, number>;
  outgoing: Record<number, OutgoingMessage[]>;
  invitations: ChannelInvitation[];
  contactRequests: PeopleResponse["requests"];
  directoryRevision: number;
  cursor: number;
  serviceStatus: ServiceStatus | null;
  roles: Role[];
  workspaceRoles: Record<number, WorkspaceRole>;
  budgetRevision: number;
  skillsRevision: number;
  models: Model[];
  band: BandStatus;
  openaiConfigured: boolean;
  llmConfigured: boolean;
  activeChannelId: number | null;
}

export const roleOf = (state: State, userId: number): WorkspaceRole => state.workspaceRoles[userId] ?? "member";
export const isAdmin = (state: State) => roleOf(state, state.me.id) !== "member";

const POLICY_REASONS: Record<keyof ChannelPolicy, string> = {
  approvers: "Only workspace admins can decide approvals in this channel.",
  computer_control: "Only workspace admins can use the live desktop in this channel.",
};

/** Why the current person can't act under a channel policy setting, or null when they can. */
export function policyBlock(state: State, channel: Channel | null | undefined, setting: keyof ChannelPolicy): string | null {
  if (!channel || channel.policy?.[setting] !== "admins" || isAdmin(state)) return null;
  return POLICY_REASONS[setting];
}

export interface CaughtUpMessages {
  channelId: number;
  messages: Message[];
  /** The conversation that was open when this page was requested. Unread follows the conversation open when the snapshot lands. */
  active: boolean;
}

export type Action =
  | { type: "event"; event: ServerEvent }
  | { type: "messages_loaded"; channelId: number; messages: Message[] }
  | { type: "tasks_loaded"; channelId: number; tasks: TaskActivity[] }
  | { type: "select_channel"; channelId: number }
  | { type: "outgoing_upsert"; message: OutgoingMessage }
  | { type: "outgoing_fail"; channelId: number; clientId: string; error: string; retryable: boolean }
  | { type: "message_settled"; message: Message }
  | { type: "people_loaded"; invitations: ChannelInvitation[]; contactRequests: PeopleResponse["requests"] }
  | { type: "status"; status: ServiceStatus }
  | { type: "reconcile"; boot: Bootstrap; events: ServerEvent[]; loaded: CaughtUpMessages[] };

const byId = <T extends { id: number }>(items: T[]) => Object.fromEntries(items.map((i) => [i.id, i]));

export function initialState(boot: Bootstrap): State {
  const general = boot.channels.find((c) => c.name === "general") ?? boot.channels.find((c) => !c.is_dm);
  return {
    me: boot.me,
    users: byId(boot.users),
    channels: byId(boot.channels),
    agents: byId(boot.agents),
    messages: {},
    activity: {},
    drafts: {},
    unread: {},
    readThrough: {},
    outgoing: {},
    invitations: boot.invitations ?? [],
    contactRequests: boot.contact_requests ?? [],
    directoryRevision: 0,
    cursor: boot.cursor ?? 0,
    serviceStatus: boot.service ?? null,
    roles: boot.roles,
    workspaceRoles: Object.fromEntries(Object.entries(boot.workspace_roles ?? {}).map(([id, role]) => [Number(id), role])),
    budgetRevision: 0,
    skillsRevision: 0,
    models: boot.models,
    band: boot.band,
    openaiConfigured: boot.openai_configured,
    llmConfigured: boot.llm_configured ?? boot.openai_configured,
    activeChannelId: general?.id ?? null,
  };
}

function mergeMessages(existing: Message[] | undefined, incoming: Message[]): Message[] {
  const map = new Map((existing ?? []).map((m) => [m.id, m]));
  for (const m of incoming) map.set(m.id, m);
  return [...map.values()].sort((a, b) => a.id - b.id);
}

function maxMessageId(messages: Message[] | undefined, floor = 0): number {
  return (messages ?? []).reduce((max, message) => Math.max(max, message.id), floor);
}

function omitRecord<T>(record: Record<number, T>, channelId: number): Record<number, T> {
  if (!(channelId in record)) return record;
  const next = { ...record };
  delete next[channelId];
  return next;
}

function dropChannel(state: State, channelId: number): State {
  return {
    ...state,
    messages: omitRecord(state.messages, channelId),
    drafts: omitRecord(state.drafts, channelId),
    activity: omitRecord(state.activity, channelId),
    unread: omitRecord(state.unread, channelId),
    readThrough: omitRecord(state.readThrough, channelId),
    outgoing: omitRecord(state.outgoing, channelId),
  };
}

function keepChannels(state: State, ids: Set<number>): State {
  const keep = <T>(record: Record<number, T>): Record<number, T> =>
    Object.fromEntries(Object.entries(record).filter(([id]) => ids.has(Number(id)))) as Record<number, T>;
  return {
    ...state,
    messages: keep(state.messages),
    drafts: keep(state.drafts),
    activity: keep(state.activity),
    unread: keep(state.unread),
    readThrough: keep(state.readThrough),
    outgoing: keep(state.outgoing),
  };
}

function dropOutgoing(state: State, channelId: number, clientId: string | null | undefined): State {
  if (!clientId) return state;
  const current = state.outgoing[channelId];
  if (!current?.some((item) => item.clientId === clientId)) return state;
  return { ...state, outgoing: { ...state.outgoing, [channelId]: current.filter((item) => item.clientId !== clientId) } };
}

function clientIdsIn(messages: Message[]): Set<string> {
  return new Set(messages.flatMap((message) => (message.client_id ? [message.client_id] : [])));
}

function withoutDrafts(state: State, channelId: number, drop: (d: Draft) => boolean): State {
  const current = state.drafts[channelId];
  if (!current || !Object.values(current).some(drop)) return state;
  const kept = Object.fromEntries(Object.entries(current).filter(([, d]) => !drop(d)));
  return { ...state, drafts: { ...state.drafts, [channelId]: kept } };
}

function rememberCursor(state: State, event: ServerEvent): State {
  if (event.cursor == null || event.cursor <= state.cursor) return state;
  return { ...state, cursor: event.cursor };
}

function applyEvent(state: State, event: ServerEvent): State {
  return rememberCursor(applyEventBody(state, event), event);
}

function applyEventBody(state: State, event: ServerEvent): State {
  switch (event.type) {
    case "document_removed": {
      const messages = state.messages[event.channel_id];
      return messages ? { ...state, messages: { ...state.messages, [event.channel_id]: messages.map((message) => ({
        ...message, attachments: message.attachments?.filter((doc) => doc.id !== event.document_id),
      })) } } : state;
    }
    case "provider_settings_changed":
      return state;
    case "openai_settings":
      return { ...state, models: event.models, openaiConfigured: event.configured };
    case "skills_changed":
      return { ...state, skillsRevision: state.skillsRevision + 1 };
    case "message": {
      const { message } = event;
      if (!state.channels[message.channel_id]) return state;
      const loaded = state.messages[message.channel_id];
      const isActive = message.channel_id === state.activeChannelId;
      const known = loaded?.some((item) => item.id === message.id) ?? false;
      const seen = message.id <= (state.readThrough[message.channel_id] ?? 0);
      const rest = dropOutgoing(
        message.author_type === "agent"
          ? withoutDrafts(state, message.channel_id, (d) => d.agent_id === message.author_id)
          : state,
        message.channel_id,
        message.client_id,
      );
      const readThrough = isActive || !known
        ? { ...rest.readThrough, [message.channel_id]: Math.max(rest.readThrough[message.channel_id] ?? 0, message.id) }
        : rest.readThrough;
      return {
        ...rest,
        messages: loaded ? { ...rest.messages, [message.channel_id]: mergeMessages(loaded, [message]) } : rest.messages,
        readThrough,
        unread: isActive || known || seen
          ? rest.unread
          : { ...rest.unread, [message.channel_id]: (rest.unread[message.channel_id] ?? 0) + 1 },
      };
    }
    case "message_updated": {
      if (!state.channels[event.message.channel_id]) return state;
      const loaded = state.messages[event.message.channel_id];
      if (!loaded?.some((m) => m.id === event.message.id)) return state;
      return { ...state, messages: { ...state.messages, [event.message.channel_id]: mergeMessages(loaded, [event.message]) } };
    }
    case "channel":
      return { ...state, channels: { ...state.channels, [event.channel.id]: event.channel } };
    case "channel_removed": {
      const channels = { ...state.channels };
      delete channels[event.channel_id];
      const next = dropChannel({ ...state, channels }, event.channel_id);
      const fallback = Object.values(channels).find((channel) => channel.name === "general" && !channel.is_dm)?.id
        ?? Object.values(channels).find((channel) => !channel.is_dm)?.id
        ?? null;
      return { ...next, activeChannelId: state.activeChannelId === event.channel_id ? fallback : state.activeChannelId };
    }
    case "user":
      return { ...state, me: event.user.id === state.me.id ? event.user : state.me,
        users: { ...state.users, [event.user.id]: event.user } };
    case "presence": {
      const user = state.users[event.user_id];
      if (!user) return state;
      return { ...state, users: { ...state.users, [user.id]: { ...user, online: event.online } } };
    }
    case "agent":
      return { ...state, agents: { ...state.agents, [event.agent.id]: event.agent } };
    case "agent_deleted": {
      const agents = { ...state.agents };
      delete agents[event.agent_id];
      const channels = Object.fromEntries(
        Object.values(state.channels)
          .filter((c) => !c.is_dm || c.dm_agent_id !== event.agent_id)
          .map((c) => [c.id, c]),
      );
      const activeChannelId =
        state.activeChannelId != null && !channels[state.activeChannelId]
          ? (Object.values(channels).find((c) => c.name === "general")?.id ?? null)
          : state.activeChannelId;
      return { ...state, agents, channels, activeChannelId };
    }
    case "agent_status": {
      const agent = state.agents[event.agent_id];
      if (!agent) return state;
      return { ...state, agents: { ...state.agents, [agent.id]: { ...agent, status: event.status } } };
    }
    case "band_status":
      return { ...state, band: event.band };
    case "task_activity": {
      if (!state.channels[event.task.channel_id]) return state;
      const { task } = event;
      const current = { ...state.activity[task.channel_id] };
      if (task.status === "done") delete current[task.id];
      else current[task.id] = task;
      const next = { ...state, activity: { ...state.activity, [task.channel_id]: current } };
      return task.status === "done" ? withoutDrafts(next, task.channel_id, (d) => d.task_id === task.id) : next;
    }
    case "draft": {
      if (!state.channels[event.channel_id]) return state;
      const { type: _type, ...draft } = event;
      if (!draft.content) return withoutDrafts(state, draft.channel_id, (d) => d.task_id === draft.task_id);
      return {
        ...state,
        drafts: { ...state.drafts, [draft.channel_id]: { ...state.drafts[draft.channel_id], [draft.task_id]: draft } },
      };
    }
    case "workspace_role": {
      const workspaceRoles = { ...state.workspaceRoles };
      if (event.role === "member") delete workspaceRoles[event.user_id];
      else workspaceRoles[event.user_id] = event.role;
      return { ...state, workspaceRoles };
    }
    case "budget":
      return { ...state, budgetRevision: state.budgetRevision + 1 };
    case "people_changed":
      return { ...state, directoryRevision: state.directoryRevision + 1 };
    case "draft_append": {
      if (!state.channels[event.channel_id]) return state;
      const current = state.drafts[event.channel_id]?.[event.task_id];
      // Only extend the exact text the append was cut from; the server resends the full draft regularly.
      if (!current || current.content.length !== event.offset) return state;
      const draft = { ...current, content: current.content + event.text };
      return {
        ...state,
        drafts: { ...state.drafts, [event.channel_id]: { ...state.drafts[event.channel_id], [event.task_id]: draft } },
      };
    }
  }
}

function settledOutgoing(state: State, channelId: number, messages: Message[]): State["outgoing"] {
  const ids = clientIdsIn(messages);
  if (ids.size === 0) return state.outgoing;
  const current = state.outgoing[channelId];
  if (!current?.some((item) => ids.has(item.clientId))) return state.outgoing;
  return { ...state.outgoing, [channelId]: current.filter((item) => !ids.has(item.clientId)) };
}

function applySnapshot(state: State, boot: Bootstrap): State {
  const channels = byId(boot.channels);
  const visible = new Set(boot.channels.map((channel) => channel.id));
  const kept = keepChannels(state, visible);
  const general = boot.channels.find((channel) => channel.name === "general" && !channel.is_dm);
  const activeChannelId = state.activeChannelId != null && channels[state.activeChannelId]
    ? state.activeChannelId
    : (general?.id ?? boot.channels.find((channel) => !channel.is_dm)?.id ?? null);
  return {
    ...kept,
    me: boot.me,
    users: byId(boot.users),
    channels,
    agents: byId(boot.agents),
    roles: boot.roles,
    workspaceRoles: Object.fromEntries(Object.entries(boot.workspace_roles ?? {}).map(([id, role]) => [Number(id), role])),
    models: boot.models,
    band: boot.band,
    openaiConfigured: boot.openai_configured,
    llmConfigured: boot.llm_configured ?? boot.openai_configured,
    activeChannelId,
    cursor: boot.cursor ?? kept.cursor,
    invitations: boot.invitations ?? kept.invitations,
    contactRequests: boot.contact_requests ?? kept.contactRequests,
    directoryRevision: kept.directoryRevision + 1,
    serviceStatus: boot.service ?? kept.serviceStatus,
  };
}

function eventChannel(event: ServerEvent): number | null {
  switch (event.type) {
    case "message":
    case "message_updated":
      return event.message.channel_id;
    case "document_removed":
    case "draft":
    case "draft_append":
    case "channel_removed":
      return event.channel_id;
    case "channel":
      return event.channel.id;
    case "task_activity":
      return event.task.channel_id;
    default:
      return null;
  }
}

/** Apply live events that are newer than the snapshot, keeping a message that arrives before its channel event. */
function applyNewerEvents(state: State, events: ServerEvent[], snapshotCursor: number): State {
  const pending: ServerEvent[] = [];
  for (const event of events) {
    if (event.cursor != null && event.cursor <= snapshotCursor) continue;
    const channelId = eventChannel(event);
    if (channelId != null && !state.channels[channelId] && event.type !== "channel" && event.type !== "channel_removed") {
      pending.push(event);
      continue;
    }
    state = applyEvent(state, event);
    if (event.type !== "channel") continue;
    const ready = pending.filter((queued) => eventChannel(queued) === event.channel.id);
    if (ready.length === 0) continue;
    for (const queued of ready) state = applyEvent(state, queued);
    const delivered = new Set(ready);
    for (let index = pending.length - 1; index >= 0; index -= 1) {
      if (delivered.has(pending[index])) pending.splice(index, 1);
    }
  }
  return state;
}

function absorbCaughtUp(state: State, batch: CaughtUpMessages): State {
  if (!state.channels[batch.channelId]) return state;
  const existing = state.messages[batch.channelId] ?? [];
  const known = new Set(existing.map((message) => message.id));
  const floor = state.readThrough[batch.channelId] ?? 0;
  const fresh = batch.messages.filter((message) => message.id > floor && !known.has(message.id));
  const merged = mergeMessages(existing, batch.messages);
  // The fetch started on one conversation. Unread follows the conversation open when the snapshot lands.
  const reading = state.activeChannelId === batch.channelId;
  return {
    ...state,
    messages: { ...state.messages, [batch.channelId]: merged },
    outgoing: settledOutgoing(state, batch.channelId, merged),
    readThrough: { ...state.readThrough, [batch.channelId]: Math.max(floor, maxMessageId(merged)) },
    unread: {
      ...state.unread,
      [batch.channelId]: reading ? 0 : (state.unread[batch.channelId] ?? 0) + fresh.length,
    },
  };
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "event":
      return applyEvent(state, action.event);
    case "messages_loaded": {
      const merged = mergeMessages(state.messages[action.channelId], action.messages);
      const active = state.activeChannelId === action.channelId;
      const floor = state.readThrough[action.channelId] ?? 0;
      return {
        ...state,
        messages: { ...state.messages, [action.channelId]: merged },
        outgoing: settledOutgoing(state, action.channelId, merged),
        readThrough: { ...state.readThrough, [action.channelId]: Math.max(floor, maxMessageId(merged)) },
        unread: active ? { ...state.unread, [action.channelId]: 0 } : state.unread,
      };
    }
    case "tasks_loaded": {
      // Polling seeds activity for clients that connected mid-task; WebSocket events keep it live.
      const active = action.tasks.filter((t) => t.status !== "done");
      const next = { ...state, activity: { ...state.activity, [action.channelId]: Object.fromEntries(active.map((t) => [t.id, t])) } };
      // A missed "done" or empty draft event (socket drop, API restart) would otherwise leave a stale bubble.
      const working = new Set(active.map((t) => t.id));
      return withoutDrafts(next, action.channelId, (d) => !working.has(d.task_id));
    }
    case "select_channel":
      return {
        ...state,
        activeChannelId: action.channelId,
        unread: { ...state.unread, [action.channelId]: 0 },
        readThrough: {
          ...state.readThrough,
          [action.channelId]: maxMessageId(state.messages[action.channelId], state.readThrough[action.channelId] ?? 0),
        },
      };
    case "outgoing_upsert": {
      const current = state.outgoing[action.message.channelId] ?? [];
      const without = current.filter((item) => item.clientId !== action.message.clientId);
      return { ...state, outgoing: { ...state.outgoing, [action.message.channelId]: [...without, action.message] } };
    }
    case "outgoing_fail": {
      const current = state.outgoing[action.channelId] ?? [];
      return {
        ...state,
        outgoing: {
          ...state.outgoing,
          [action.channelId]: current.map((item) => item.clientId === action.clientId
            ? { ...item, status: "failed", error: action.error, retryable: action.retryable }
            : item),
        },
      };
    }
    case "people_loaded":
      return { ...state, invitations: action.invitations, contactRequests: action.contactRequests };
    case "status":
      return { ...state, serviceStatus: action.status };
    case "message_settled": {
      const message = action.message;
      const merged = mergeMessages(state.messages[message.channel_id], [message]);
      const active = state.activeChannelId === message.channel_id;
      return dropOutgoing({
        ...state,
        messages: { ...state.messages, [message.channel_id]: merged },
        readThrough: {
          ...state.readThrough,
          [message.channel_id]: Math.max(state.readThrough[message.channel_id] ?? 0, message.id),
        },
        unread: active ? { ...state.unread, [message.channel_id]: 0 } : state.unread,
      }, message.channel_id, message.client_id);
    }
    case "reconcile": {
      let next = applySnapshot(state, action.boot);
      next = applyNewerEvents(next, action.events, action.boot.cursor ?? 0);
      for (const batch of action.loaded) next = absorbCaughtUp(next, batch);
      if (next.activeChannelId != null && next.channels[next.activeChannelId]) {
        next = { ...next, unread: { ...next.unread, [next.activeChannelId]: 0 } };
      }
      return next;
    }
  }
}
