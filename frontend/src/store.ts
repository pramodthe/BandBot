import type {
  Agent,
  BandStatus,
  Bootstrap,
  Channel,
  ChannelPolicy,
  Draft,
  Message,
  Model,
  Role,
  ServerEvent,
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
  typing: Record<number, number[]>;
  activity: Record<number, Record<string, TaskActivity>>;
  drafts: Record<number, Record<string, Draft>>;
  unread: Record<number, number>;
  roles: Role[];
  workspaceRoles: Record<number, WorkspaceRole>;
  budgetRevision: number;
  skillsRevision: number;
  models: Model[];
  band: BandStatus;
  openaiConfigured: boolean;
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

export type Action =
  | { type: "event"; event: ServerEvent }
  | { type: "messages_loaded"; channelId: number; messages: Message[] }
  | { type: "tasks_loaded"; channelId: number; tasks: TaskActivity[] }
  | { type: "select_channel"; channelId: number };

const byId = <T extends { id: number }>(items: T[]) => Object.fromEntries(items.map((i) => [i.id, i]));

export function initialState(boot: Bootstrap): State {
  const general = boot.channels.find((c) => c.name === "general") ?? boot.channels.find((c) => !c.is_dm);
  return {
    me: boot.me,
    users: byId(boot.users),
    channels: byId(boot.channels),
    agents: byId(boot.agents),
    messages: {},
    typing: {},
    activity: {},
    drafts: {},
    unread: {},
    roles: boot.roles,
    workspaceRoles: Object.fromEntries(Object.entries(boot.workspace_roles ?? {}).map(([id, role]) => [Number(id), role])),
    budgetRevision: 0,
    skillsRevision: 0,
    models: boot.models,
    band: boot.band,
    openaiConfigured: boot.openai_configured,
    activeChannelId: general?.id ?? null,
  };
}

function mergeMessages(existing: Message[] | undefined, incoming: Message[]): Message[] {
  const map = new Map((existing ?? []).map((m) => [m.id, m]));
  for (const m of incoming) map.set(m.id, m);
  return [...map.values()].sort((a, b) => a.id - b.id);
}

function withoutDrafts(state: State, channelId: number, drop: (d: Draft) => boolean): State {
  const current = state.drafts[channelId];
  if (!current || !Object.values(current).some(drop)) return state;
  const kept = Object.fromEntries(Object.entries(current).filter(([, d]) => !drop(d)));
  return { ...state, drafts: { ...state.drafts, [channelId]: kept } };
}

function applyEvent(state: State, event: ServerEvent): State {
  switch (event.type) {
    case "document_removed": {
      const messages = state.messages[event.channel_id];
      return messages ? { ...state, messages: { ...state.messages, [event.channel_id]: messages.map((message) => ({
        ...message, attachments: message.attachments?.filter((doc) => doc.id !== event.document_id),
      })) } } : state;
    }
    case "openai_settings":
      return { ...state, models: event.models, openaiConfigured: event.configured };
    case "skills_changed":
      return { ...state, skillsRevision: state.skillsRevision + 1 };
    case "message": {
      const { message } = event;
      const loaded = state.messages[message.channel_id];
      const isActive = message.channel_id === state.activeChannelId;
      // An agent runs one task per channel at a time, so its message replaces its draft there.
      const rest =
        message.author_type === "agent"
          ? withoutDrafts(state, message.channel_id, (d) => d.agent_id === message.author_id)
          : state;
      return {
        ...rest,
        messages: loaded ? { ...state.messages, [message.channel_id]: mergeMessages(loaded, [message]) } : state.messages,
        unread: isActive
          ? state.unread
          : { ...state.unread, [message.channel_id]: (state.unread[message.channel_id] ?? 0) + 1 },
      };
    }
    case "message_updated": {
      const loaded = state.messages[event.message.channel_id];
      if (!loaded?.some((m) => m.id === event.message.id)) return state;
      return { ...state, messages: { ...state.messages, [event.message.channel_id]: mergeMessages(loaded, [event.message]) } };
    }
    case "channel":
      return { ...state, channels: { ...state.channels, [event.channel.id]: event.channel } };
    case "user":
      return { ...state, users: { ...state.users, [event.user.id]: event.user } };
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
          .filter((c) => !c.is_dm || (c.dm_agent_id !== null && c.dm_agent_id !== event.agent_id))
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
    case "typing": {
      const current = (state.typing[event.channel_id] ?? []).filter((id) => id !== event.agent_id);
      return {
        ...state,
        typing: { ...state.typing, [event.channel_id]: event.active ? [...current, event.agent_id] : current },
      };
    }
    case "band_status":
      return { ...state, band: event.band };
    case "task_activity": {
      const { task } = event;
      const current = { ...state.activity[task.channel_id] };
      if (task.status === "done") delete current[task.id];
      else current[task.id] = task;
      const next = { ...state, activity: { ...state.activity, [task.channel_id]: current } };
      return task.status === "done" ? withoutDrafts(next, task.channel_id, (d) => d.task_id === task.id) : next;
    }
    case "draft": {
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
    case "draft_append": {
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

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "event":
      return applyEvent(state, action.event);
    case "messages_loaded":
      return {
        ...state,
        messages: { ...state.messages, [action.channelId]: mergeMessages(state.messages[action.channelId], action.messages) },
      };
    case "tasks_loaded": {
      // Polling seeds activity for clients that connected mid-task; WebSocket events keep it live.
      const active = action.tasks.filter((t) => t.status !== "done");
      const next = { ...state, activity: { ...state.activity, [action.channelId]: Object.fromEntries(active.map((t) => [t.id, t])) } };
      // A missed "done" or empty draft event (socket drop, API restart) would otherwise leave a stale bubble.
      const working = new Set(active.map((t) => t.id));
      return withoutDrafts(next, action.channelId, (d) => !working.has(d.task_id));
    }
    case "select_channel":
      return { ...state, activeChannelId: action.channelId, unread: { ...state.unread, [action.channelId]: 0 } };
  }
}
