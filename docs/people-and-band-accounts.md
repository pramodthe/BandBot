# People, contacts, and shared channels

New accounts require each person’s own Band Agent API key (`band_a_…`) during signup. The app validates the key against Band and stores it encrypted as that user’s platform connection. Existing users can sign in as before and add or update their connection in People. Signup does not create an AI teammate. The server Band key stays on the server. Browsing or inviting that account's other agents is limited to workspace admins.

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

The operator's workspace Band connection supplies the shared agents and their transport. A member can chat with those agents without a Band key. Creating another agent on that same account is limited to workspace admins. Human collaboration continues when Band is offline. A channel without a Band room can acquire one when an agent is explicitly added and a connection is available.

Channel messages mirrored to Band use the room's owning connection with the app author's display name. Agent routing and model execution remain in the separate worker. Human app members do not automatically become native Band participants. App friendship and native Band contact consent are separate relationships.

Individual users connect the app to Band during signup using their own **Band Agent API key** (`band_a_…`). Create the connection identity in [Band’s dashboard](https://app.band.ai/agents) first. The key is validated against the Agent API, encrypted on the server, and bound to the signed-in app user. Signup stores a platform connection separately from AI teammate profiles. Choose **Add agents → Create agent** to create teammates with their own instructions, memory, and tasks using that saved connection. No additional Band key is needed. Existing users can add the connection in People, where key updates must retain the same Band identity.

New Band rooms and outgoing results use the room owner's saved Agent API connection. Incoming messages are accepted only for rooms pinned to that connection and from native room participants. An unavailable or revoked personal key never falls back to another user's or the operator's credential. Internal report-back and interim teammate replies stay local; the final answer is delivered to Band once. Unconfirmed sends remain visible for review rather than being retried automatically.

Existing accounts can also import a separate Band identity through **Add agents → Connect Band agent** with that identity's own key. These teammates use durable tasks and the existing model/approval policies. Conversations stay local in legacy app rooms; rooms using a saved platform connection can relay through the room owner's Agent API credential. Removing an imported teammate preserves its original Band identity. An existing Band identity can only be imported once in a workspace; other users cannot manage it through a shared channel. Removing a platform teammate also preserves the user's saved connection and native Band identity.

The **Advanced: Band Human API (Enterprise)** section in People remains available for Enterprise accounts that need programmatic registration and human room mirroring. This is separate from the individual agent-key flow. Existing rooms and registered agents keep their original account. A Band human identity can be linked to one app user, and key updates must retain that identity. Human keys are encrypted and never returned to the browser.

Band's own contacts can be managed independently in Band. Bringing agents owned by different native Band accounts into a Band room also follows Band's own contact and participant permissions.

Codex, Claude, Cursor, and OpenClaw authentication is separate again: users connect their own provider credentials to run those agents. Provider connections belong to their creator. Custom agents use the configured workspace model provider. Workspace app access and skills remain governed by admin policies.

Runtime installation, model billing, MCP services, E2B and workspace roles remain operator-managed. This is a shared installation, not separate hosted execution environments or tenant isolation. Run one API process and one worker with the same database, checkpoints, environment, and stable `APP_SECRET`. Unconfirmed Band sends are not blindly retried.
