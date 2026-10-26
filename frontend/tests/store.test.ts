import assert from "node:assert/strict";
import test from "node:test";
import { initialState, reducer, type State } from "../src/store.ts";
import type { Bootstrap, Channel, Message, ServerEvent } from "../src/types.ts";

function channel(id: number, name: string): Channel {
  return {
    id, name, topic: "", band_room_id: null, human_ids: [1], agent_ids: [], outside_agents: [],
    is_dm: false, dm_agent_id: null, policy: { approvers: "members", computer_control: "members" },
  };
}

function message(id: number, channelId: number, content: string, clientId?: string): Message {
  return {
    id, channel_id: channelId, author_type: "human", author_id: 2, author_name: "Bob", content,
    mentions: [], to_humans: true, components: [], approval: null, handoff: false,
    created_at: "2026-10-21T00:00:00Z", ...(clientId ? { client_id: clientId } : {}),
  };
}

function boot(channels: Channel[], cursor = 1, invitations: Bootstrap["invitations"] = []): Bootstrap {
  return {
    me: { id: 1, username: "alice", display_name: "Alice", online: true },
    users: [
      { id: 1, username: "alice", display_name: "Alice", online: true },
      { id: 2, username: "bob", display_name: "Bob", online: false },
    ],
    channels, agents: [], roles: [], workspace_roles: {}, models: [],
    band: { configured: false, online: false, owner_name: null, owner_handle: null, error: null },
    openai_configured: false, cursor, invitations, contact_requests: [],
  };
}

function withMessages(state: State, channelId: number, messages: Message[]): State {
  return reducer(state, { type: "messages_loaded", channelId, messages });
}

test("reconnect drops a channel the server no longer allows, even if an older event includes it", () => {
  const general = channel(1, "general");
  const secret = channel(2, "launch");
  let state = initialState(boot([general, secret], 4));
  state = reducer(state, { type: "select_channel", channelId: 2 });
  state = withMessages(state, 2, [message(7, 2, "hidden")]);
  const stale: ServerEvent = { type: "channel", channel: { ...secret, topic: "stale" }, cursor: 4 };
  state = reducer(state, {
    type: "reconcile",
    boot: boot([general], 5, [{ id: "inv", channel_name: "launch", inviter_name: "Bob" }]),
    events: [stale],
    loaded: [],
  });
  assert.equal(state.channels[2], undefined);
  assert.equal(state.messages[2], undefined);
  assert.equal(state.activeChannelId, 1);
  assert.equal(state.invitations[0]?.channel_name, "launch");
});

test("a newer removal wins over a snapshot that still lists the channel", () => {
  const general = channel(1, "general");
  const secret = channel(2, "launch");
  let state = initialState(boot([general, secret], 5));
  state = reducer(state, { type: "select_channel", channelId: 2 });
  state = reducer(state, {
    type: "reconcile",
    boot: boot([general, secret], 5),
    events: [{ type: "channel_removed", channel_id: 2, cursor: 6 }],
    loaded: [],
  });
  assert.equal(state.channels[2], undefined);
  assert.equal(state.activeChannelId, 1);
  assert.equal(state.cursor, 6);
});

test("catch-up and a repeated live message count unread once and keep order", () => {
  const general = channel(1, "general");
  const other = channel(3, "other");
  let state = initialState(boot([general, other]));
  state = reducer(state, { type: "select_channel", channelId: 3 });
  state = withMessages(state, 1, [message(1, 1, "first"), message(3, 1, "third")]);
  const arrived = message(4, 1, "second wave");
  state = reducer(state, {
    type: "reconcile",
    boot: boot([general, other], 2),
    events: [{ type: "message", message: arrived, cursor: 3 }],
    loaded: [{ channelId: 1, messages: [message(2, 1, "between"), arrived], active: false }],
  });
  assert.deepEqual(state.messages[1]?.map((item) => item.id), [1, 2, 3, 4]);
  assert.equal(state.unread[1], 1);
  state = reducer(state, { type: "event", event: { type: "message", message: arrived, cursor: 4 } });
  assert.equal(state.unread[1], 1);
  assert.equal(state.unread[3], 0);
});

test("catch-up still counts unread when the reader has switched away", () => {
  const general = channel(1, "general");
  const other = channel(3, "other");
  let state = initialState(boot([general, other]));
  state = withMessages(state, 1, [message(1, 1, "seen")]);
  state = reducer(state, { type: "select_channel", channelId: 3 });
  state = reducer(state, {
    type: "reconcile",
    boot: boot([general, other], 2),
    events: [],
    loaded: [{ channelId: 1, messages: [message(1, 1, "seen"), message(5, 1, "while away")], active: true }],
  });
  assert.equal(state.unread[1], 1);
  assert.equal(state.unread[3], 0);
  assert.deepEqual(state.messages[1]?.map((item) => item.id), [1, 5]);
});

test("a settled retry replaces the outgoing bubble without another unread", () => {
  const general = channel(1, "general");
  let state = initialState(boot([general]));
  state = reducer(state, { type: "select_channel", channelId: 1 });
  state = reducer(state, {
    type: "outgoing_upsert",
    message: {
      clientId: "client-1", channelId: 1, content: "hello", attachmentIds: [],
      createdAt: "2026-10-21T00:00:00Z", status: "pending", retryable: true,
    },
  });
  state = reducer(state, { type: "message_settled", message: message(8, 1, "hello", "client-1") });
  assert.equal(state.outgoing[1]?.length ?? 0, 0);
  assert.equal(state.messages[1]?.[0]?.content, "hello");
  assert.equal(state.unread[1], 0);
  state = reducer(state, { type: "event", event: { type: "message", message: message(8, 1, "hello", "client-1"), cursor: 3 } });
  assert.equal(state.unread[1], 0);
});

test("people changes refresh the directory without restoring a removed channel", () => {
  const general = channel(1, "general");
  const secret = channel(2, "launch");
  let state = initialState(boot([general, secret]));
  state = reducer(state, { type: "event", event: { type: "channel_removed", channel_id: 2 } });
  const revision = state.directoryRevision;
  state = reducer(state, { type: "event", event: { type: "people_changed" } });
  assert.equal(state.channels[2], undefined);
  assert.equal(state.directoryRevision, revision + 1);
  state = reducer(state, {
    type: "people_loaded",
    invitations: [{ id: "inv", channel_name: "notes", inviter_name: "Bob" }],
    contactRequests: [],
  });
  assert.equal(state.channels[2], undefined);
  assert.equal(state.invitations[0]?.channel_name, "notes");
});
