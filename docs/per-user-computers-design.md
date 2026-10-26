# Per-user computers with independent agent desktops

Status: implemented for new Tenki tasks. Live checks confirmed shared files and separate X displays for two agents owned by A, a separate VM/filesystem for B, and successful desktop/network/files/pause-resume verification with 1 GiB data disks. Historical resources remain pinned.

## Requested behavior

- User A owns one current Tenki VM and persistent data disk for A's agents.
- User B owns a different VM and disk. No computer, disk, browser profile, or ingress credential is shared between app users.
- Each agent has an independent graphical desktop, mouse/keyboard state, and browser profile within its owner's VM.
- All of one user's agents can collaborate through shared persistent project files. Each agent can also keep its own working directory and desktop/browser state.
- Shared files and installed tools are intentionally accessible to sibling agents owned by that user. A folder named private is not an access-control boundary between those agents.
- Existing application memory remains scoped to agent and channel. Shared files can contain durable project notes without merging agent memory automatically.
- This requires independent desktop sessions; multiple viewer windows attached to the same display do not satisfy the requirement.
- Joining a group channel grants no access to another user's computer, screenshots, live desktop, browser state, or private files. Owners inspect their own desktops through a private computer panel; group conversations share messages and deliberately published outputs.

## Implementation and retained boundaries

Tenki documents full Linux VMs, custom images, command execution, persistent volumes, and pause/resume: [Tenki Sandbox](https://tenki.cloud/products/sandbox), [SDK reference](https://tenki.cloud/docs/sandbox/sdk).

The existing image installs Xvfb, XFCE, x11vnc, and websockify. X display addressing distinguishes display instances: [X.Org display concepts](https://www.x.org/guide/concepts/). Separate Xvfb servers now provide independent input and graphical sessions, validated together in the configured Tenki image.

The new `user` mode binds each physical host to an immutable user identity plus provider revision. `UserDesktop` assigns an independent display and VNC port pair to each agent. Screenshot frames, takeover, and presentation state remain per desktop. Legacy workspace and agent/channel modes retain their original task bindings.

Computer discovery, cached screenshots, live connections, file operations, app previews, and ongoing viewer authorization now check computer ownership in addition to channel membership/policy. `TaskPrincipal` retains the originating local user across hand-offs and schedules. Requests from another user or an unknown native sender cannot use the private host. Group computer listings hide user desktops, and nonowners receive no private audit details. Group file export requires owner approval and produces a bounded copy; interactive app previews stay private.

Independent browser sessions require separate browser profiles. Sharing files and installed tools does not automatically share browser cookies or logins. Concurrently opening the same Chrome profile is not a supported isolation strategy. Shared browser authentication needs a deliberate, owner-scoped mechanism and live validation before it is promised.

## Implementation boundaries

1. Separate the physical computer host from an agent desktop. The host belongs to an immutable local user identity and a pinned provider connection/revision. Each desktop belongs to that host and an immutable agent identity.
2. Allocate and persist a separate display and control/view-only VNC port pair for every desktop. Route browser profiles, screenshots, input, and live viewers to that desktop. Do not expose arbitrary guest ports.
3. Keep shared project files on the owner's durable volume. Put agent-specific profiles and state in separate directories on that volume. Keep provider credentials on the app server.
4. Serialize VM creation, resume, pause, and disk operations with a host-level cross-process lock. Serialize input and screenshot observations per desktop. One task must not pause a host used by another desktop, task, viewer, or human takeover.
5. Apply takeover to the selected desktop, with fresh screenshot checks and per-request viewer authorization. File, screen, and viewer access must enforce immutable local-user ownership on the server. Channel membership or an administrator role must not implicitly grant another user's private computer access.
6. Preserve task checkpoints, old computer bindings, existing VMs, and existing disks. Activate the new mode only for new work; never silently move private files or repin a saved task to a different computer.

## Group-channel security: resolved requirements

- A's computers and filesystem remain private to A and A's agents. B and B's agents cannot mount, browse, view, control, or receive private computer credentials for them, even when the agents share a group channel.
- Remove private computer viewers, takeover controls, snapshots, and raw filesystem browsing from group-channel surfaces. An authenticated owner can access their own agents through a separate private panel. Hiding controls supplements server authorization; it does not replace it.
- Share finished outputs by explicitly publishing a bounded artifact copy into the channel. A published artifact must not grant a private filesystem path, live file browser, or reusable computer credential. Preserve channel membership checks for the published copy.
- Record the requesting local user and authorization scope durably on tasks, including hand-offs, schedules, approvals, and checkpoint resumes. A request from B must not silently gain A's private-computer privileges through A's agent. Require A's authorization for private-resource use or publication outside an already approved scope; otherwise constrain the task to shared channel inputs. Prompt instructions alone cannot enforce this.
- Keep memory and private chat isolated by agent and channel. Shared group messages and deliberately published results remain visible according to channel membership.

## Band controls and application responsibilities

Band documents participant checks for room subscriptions and identity restrictions for agent subscriptions: [Subscriptions API](https://docs.band.ai/websocket/overview). It also documents consent-based cross-boundary contacts and visibility/registry settings: [Contacts & Discovery](https://docs.band.ai/core-concepts/contacts). Mention routing and room-scoped executions reduce unrelated conversation context: [Chat Rooms & Routing](https://docs.band.ai/core-concepts/chat-rooms), [Agents](https://docs.band.ai/core-concepts/agents).

Band's documentation states that remote-agent models, tools, and infrastructure are controlled by the integrating application. Therefore these messaging controls must not be treated as Tenki filesystem, desktop, or export authorization. Our local users, channels, task permissions, computer endpoints, and output publication must enforce their own server-side boundaries. This design does not require implementing Band's Human API.

## Verification and ongoing checks

- Two agents owned by A bind to one VM/disk but different display sessions; B's agents bind to a separate VM/disk.
- A's agents read the same shared project file. Each agent retains its own browser session and cannot accidentally send input to another desktop.
- Concurrent input, screenshots, live streams, and takeover remain separated by desktop. One agent finishing does not pause another's work.
- Pause/resume retains files and both desktop sessions. A replacement VM reuses only the verified owner's durable disk and recreates sessions without claiming that RAM survived replacement.
- Server access checks reject cross-user access, including in a shared channel. App approvals and secret masking remain intact.
- B cannot use group prompts, agent hand-offs, scheduled work, or checkpoint resumes to read or publish A's private files without A's authorization. Published artifact copies remain accessible only to authorized channel members.
- Group events, task details, cached observations, and live-view tokens do not leak private screenshots, paths, credentials, or unpublished files.
- Existing historical tasks and computers remain usable under their pinned mode. New task creation does not allocate duplicate hosts or disks after an uncertain provider request.
- Measure RAM/CPU use with multiple graphical desktops. Sharing one VM reduces VM count, but additional desktop/browser sessions still consume resources.

Live validation on the configured Tenki account used User A's Live Check A and Master (one VM, displays 1 and 2), and User B's ValueScout (another VM, display 1). A shared sentinel file was readable by A's sibling agent and absent from B's filesystem. Independent X root properties confirmed that the screens were separate X servers. Idle computers were paused after the checks. Existing 20 GiB volumes were retained; selecting 1 GiB affects new volumes only.

A live sample with two Xvfb servers, two XFCE sessions and both Chrome profiles open used about 1,233 MiB of a 3,887 MiB guest, with 2,654 MiB available. This is an idle desktop sample, not a production concurrency or load benchmark.
