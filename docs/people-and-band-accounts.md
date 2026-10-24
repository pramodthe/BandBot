# People, contacts, and shared channels

New accounts and human chat require no Band key. Each user can optionally connect their own `band_u_…` user key at signup or in People to create and manage native Band agents. Keys are validated, encrypted, and scoped to that app user. There is no server-wide fallback key. Signup does not create an AI teammate.

Open **People** using the person-plus navigation button or the user menu.

1. Find someone by their app username, or choose **Add** in the workspace directory.
2. The recipient opens **People** and accepts the contact request. Pending requests grant no conversation access.
3. Choose **Message** to open a private conversation containing only the two people. Human direct messages stay in the app database and WebSocket service; they are not sent to Band or any AI agent. Typing an agent name in these conversations does not create agent work.
4. Create a channel, open its member panel, and choose **Invite people**. Invitations require an accepted app contact. The recipient accepts in **People** before they can read history or send messages. Members can leave; the channel owner remains responsible for the channel.
5. Add AI teammates when needed. Invited humans can address those agents in the shared channel, but cannot privately DM, manage, or reshare someone else's owned agent.

Contacts, requests, memberships and messages use the application database (SQLite by default, or PostgreSQL when `DATABASE_URL` points there). The server enforces access for bootstrap, history, tasks, documents, usage, and WebSocket events. New channels use explicit membership and start without agents. Existing shared-workspace channels retain their original access.

While People is closed, a contact request or channel invitation raises the People badge and a row in the conversation list. Those updates arrive on the WebSocket for the two people involved and are loaded again after a reconnect. A third person does not receive the event.

The sender can **Cancel** a contact request that is still pending. The channel owner can **Withdraw** a pending invitation. Accepting after either of those does nothing: no contact and no membership is created. Deciding the same request again is safe.

**Remove** deletes the contact. Shared channels, memberships, and history remain. Opening a new direct message requires a new accepted request.

**Block** stops new contact requests, new direct messages, and new invitations in both directions. It does not remove someone from a channel they already share, and it does not delete history. An existing direct message stays visible with its history, and its composer explains that messaging is unavailable. A shared channel continues to accept messages. **Unblock** removes only your block.

The sample Alice and Bob accounts exchanging private messages:

![Private human conversation](assets/people-private-chat.png)

After Alice invites Bob into a channel with Master, Bob can address the agent there without a personal Band connection:

![Bob talking to Master in Alice's channel](assets/people-shared-agent-chat.png)

## Band and agent ownership

Use **People → Create and manage Band agents** to save your Band user key (`band_u_…`) from Band Settings → API Keys. **Add agents → Create agent** registers an identity in your Band account and saves its separate Agent API key (`band_a_…`) for runtime messaging. Agent profiles, instructions, memory, tasks, approvals, and computers stay separate. User A never uses B's credentials, and failed requests never fall back to a workspace environment key.

Band calls native registration and deletion the Human API; these agent-management calls are supported. Native Band human-chat synchronization is **coming soon**. Human app accounts, friendship, invitations, membership and messages remain local, and human DMs are never relayed to Band or an agent.

Existing Agent API connections and imported native identities remain supported. Compatibility app teammates may share a saved Agent API connection without registering another native identity. Imported agents are disconnected locally, preserving native ownership; deleting a native identity created by the app uses its original owner's management key. Shared transport identities are preserved.

Local channels can contain people and agents from both users without native Band human memberships. Their messages and agent tasks are saved atomically. Agent reasoning runs in the worker and results appear through our WebSocket service. When an existing native room is connected, Agent API sends use its pinned identity; native incoming messages are restricted to mapped rooms and participants. Native sends with uncertain receipts are not blindly retried.

Historical rooms and conversation history remain intact. Older native rooms without an Agent API binding do not silently use a different connection. App friendship and native Band contacts are separate relationships. Provider credentials are configured independently of Band keys.
