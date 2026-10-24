import type { ChartVariant } from "./components/assistant-ui/elements/chart";
import type { DataTableColumn, DataTableRow } from "./components/assistant-ui/elements/data-table";
import type { SpecRow } from "./components/assistant-ui/elements/spec-sheet";
import type { ComputerStep } from "./components/assistant-ui/elements/computer-use";

export interface User {
  id: number;
  username: string;
  display_name: string;
  online: boolean;
}

export interface Channel {
  id: number;
  name: string;
  topic: string;
  band_room_id: string | null;
  human_ids: number[];
  agent_ids: number[];
  outside_agents: OutsideAgent[];
  is_dm: boolean;
  dm_agent_id: number | null;
  dm_user_ids?: number[];
  owner_id?: number | null;
  private?: boolean;
  policy: ChannelPolicy;
  /** Human direct messages are closed after a block. Shared channels stay open and history stays. */
  messaging_disabled?: boolean;
}

export type WorkspaceRole = "owner" | "admin" | "member";

export interface ChannelPolicy {
  approvers: "members" | "admins";
  computer_control: "members" | "admins";
}

export interface OutsideAgent {
  band_id: string;
  handle: string;
  name: string;
  description: string;
}

export interface BandPeer extends OutsideAgent {
  online: boolean;
  is_external: boolean;
}

export interface ChannelComputer {
  agent_id: number;
  task_id: string;
  computer_key?: string;
  status: string;
  last_action: string;
  updated_at: string;
  agent_working: boolean;
  computer_working: boolean;
}

export interface AgentStatus {
  state: "starting" | "online" | "stopped" | "error";
  error: string | null;
}

export interface AgentComputerScope {
  computer_key?: string;
  shared?: boolean;
  provider?: "e2b" | "tenki";
  configured?: boolean;
  channel_id: number;
  channel_name: string;
  is_dm: boolean;
  task_id: string | null;
  status: string;
  updated_at: string | null;
  agent_working: boolean;
  can_control: boolean;
}

export interface AgentComputersResponse {
  configured: boolean;
  computers: AgentComputerScope[];
}

export type AgentRuntime = "deepagents" | "codex" | "claude" | "cursor" | "openclaw";
export type ConnectedAgentRuntime = Exclude<AgentRuntime, "deepagents">;

export interface AgentConnection {
  id: string;
  provider: ConnectedAgentRuntime;
  label: string;
  auth_method: "api_key" | "chatgpt" | "codex_chatgpt" | "gateway_token";
  models: string[];
}

export interface AgentConnectionsResponse {
  connections: AgentConnection[];
  codex_chatgpt_sign_in: boolean;
  codex_local_sign_in: boolean;
  runtimes: Record<ConnectedAgentRuntime, boolean>;
  openclaw_gateways: string[];
}

export interface Agent {
  band_auth?: "agent_key" | "human_api" | "platform";
  model_ref?: { connection_id: string; revision: number; provider: string; model: string } | null;
  id: number;
  owner_id?: number | null;
  name: string;
  handle: string;
  role: string;
  role_label: string;
  specialty?: string;
  avatar?: string | null;
  instructions: string;
  model: string;
  runtime?: AgentRuntime;
  connection_id?: string | null;
  band_agent_id: string;
  is_master: boolean;
  status: AgentStatus;
  created_by: number | null;
  created_by_agent: number | null;
  created_at?: string;
  /** Null means every plugin connected in the workspace. */
  plugins: string[] | null;
}

export interface BandAccount {
  connected: boolean;
  handle: string;
  name: string;
}

export interface BandPlatformConnection {
  connected: boolean;
  online?: boolean;
  error?: string | null;
  name: string;
  handle: string;
  api: "agent";
  verified_at: string | null;
}

export interface PeopleResponse {
  contacts: { user_id: number; name: string; handle: string }[];
  requests: { id: string; user_id: number; name: string; handle: string; message: string }[];
  sent_requests: { id: string; user_id: number; name: string; handle: string; message: string }[];
  sent_invitations?: { id: string; channel_name: string; recipient_name: string }[];
  blocked?: { user_id: number; name: string; handle: string }[];
}

export interface ServiceProvider {
  agent_id: number;
  name: string;
  provider: string;
  state: "ready" | "sign_in_required" | "unavailable" | string;
  action?: "reconnect";
}

export interface ServiceStatus {
  api: string;
  human_chat: string;
  worker: "ready" | "busy" | "starting" | "unavailable" | string;
  providers: ServiceProvider[];
}

export interface ChannelInvitation {
  id: string;
  channel_name: string;
  inviter_name: string;
}

export interface Plugin {
  slug: string;
  name: string;
  description: string;
  logo: string | null;
  connected: boolean;
}

export interface SkillSummary {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  created_by_name: string;
  file_count: number;
  updated_at: string;
}

export interface SkillDetail extends SkillSummary {
  instructions: string;
  files: Record<string, string>;
}

export interface AgentSkillsResponse {
  library: SkillSummary[];
  installed: { skill_id: string; enabled: boolean }[];
}

export interface PluginList {
  configured: boolean;
  plugins: Plugin[];
}

export interface Mention {
  type: "agent" | "human" | "external";
  id: number | null;
  name: string;
}

export interface Message {
  attachments?: WorkspaceDocument[];
  artifacts?: ArtifactReference[];
  id: number;
  channel_id: number;
  author_type: "human" | "agent" | "system" | "external";
  author_id: number | null;
  author_name: string;
  content: string;
  mentions: Mention[];
  to_humans: boolean;
  components: MessageComponent[];
  approval: MessageApproval | null;
  handoff: boolean;
  created_at: string;
  /** Present when this human send used a client idempotency key. */
  client_id?: string;
}

export interface OutgoingMessage {
  clientId: string;
  channelId: number;
  content: string;
  attachmentIds: string[];
  createdAt: string;
  status: "pending" | "failed";
  error?: string;
  retryable: boolean;
}

export interface MessageApproval {
  id: string;
  status: "pending" | "approved" | "denied";
  action: string;
  details: string;
  decided_by: string;
  note: string;
}

export interface ImageGenerationActivity {
  id: string;
  task_id: string;
  agent_id: number;
  title: string;
  size: string;
  status: "generating" | "ready" | "error";
  document_id: string | null;
  model: string;
  error: string;
}

export type MessageComponent =
  | { id: number; kind: "image"; props: { documentId: string; title: string; model: string } }
  | { id: number; kind: "sandbox"; props: SandboxedUISurface }
  | { id: number; kind: "app"; props: { previewId: string; title: string; port: number } }
  | { id: number; kind: "ui"; props: AssistantUISurface }
  | { id: number; kind: "input"; props: AssistantUISurface; request: InputRequest; response?: InputReceipt | null }
  | { id: number; kind: "form"; props: GeneratedForm; response?: FormResponse | null }
  | { id: number; kind: "view"; props: GeneratedView }
  | { id: number; kind: "table"; props: { caption: string; columns: DataTableColumn[]; rows: DataTableRow[] } }
  | {
      id: number;
      kind: "chart";
      props: {
        label: string;
        value: string;
        points: number[];
        visibleCount: number;
        delta?: string;
        trend?: "up" | "down" | "flat";
        upIsGood?: boolean;
        variant?: ChartVariant;
      };
    }
  | { id: number; kind: "spec"; props: { title: string; subtitle?: string; rows: SpecRow[]; visibleCount: number } };

export interface GeneratedForm {
  title: string;
  description: string;
  fields: { id: string; label: string; type: "text" | "textarea" | "number" | "select" | "checkbox" | "date"; required: boolean; placeholder: string; options: string[] }[];
  actions: { id: string; label: string; prompt: string; variant: "primary" | "secondary" }[];
}

export interface FormResponse {
  actionId: string;
  label: string;
  userName: string;
  values: Record<string, string | number | boolean>;
  messageId: number;
  createdAt: string;
}

export type ViewBlock =
  | { type: "text"; title: string; body: string }
  | { type: "metrics"; items: { label: string; value: string; detail: string }[] }
  | { type: "chart"; label: string; value: string; points: number[]; delta?: string; variant: ChartVariant }
  | { type: "table"; title: string; columns: DataTableColumn[]; rows: DataTableRow[] }
  | { type: "list"; title: string; items: { label: string; detail: string; status: "neutral" | "success" | "warning" | "danger" }[] }
  | { type: "progress"; title: string; items: { label: string; value: number }[] };

export interface GeneratedView {
  title: string;
  description: string;
  blocks: ViewBlock[];
  source: string;
}

export interface AssistantUISurface {
  title: string;
  description: string;
  ui: Record<string, unknown>;
  required?: string[];
}

export interface SandboxedUISurface {
  title: string;
  description: string;
  initialHeight: number;
  placeholderMessages: string[];
  css: string;
  html: string;
  jsFunctions: string;
  jsExpressions: string;
}

export interface InputRequest {
  id: string;
  taskId: string;
  status: "pending" | "answered" | "closed";
}

export interface InputReceipt {
  userName: string;
  values: { answers: Record<string, unknown>; steering: string };
}

export type ChannelGeneratedView = {
  id: number;
  message_id: number;
  author_name: string;
  created_at: string;
} & ({ kind: "view"; props: GeneratedView } | { kind: "ui"; props: AssistantUISurface } | { kind: "sandbox"; props: SandboxedUISurface });

export interface TaskActivity {
  id: string;
  channel_id: number;
  agent_id: number;
  status: "queued" | "running" | "waiting" | "done";
  progress: string;
}

export interface Task {
  restarted_from?: string;
  provider_block?: { service: string; code: string; connection_id: string; detail: string };
  waiting_for?: "input" | "approval";
  id: string;
  agent_id: number;
  status: "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled";
  progress: string;
  error: string | null;
  attempts: number;
  cancel_requested: boolean;
  delivery_status: string;
  created_at: string;
  updated_at: string;
  usage?: TaskUsage | null;
}

export interface TaskUsage {
  input_tokens: number;
  output_tokens: number;
  cost: number | null;
}

export interface AgentUsage {
  agent_id: number;
  tokens: number;
  cost: number | null;
  agent_tokens_today: number;
  agent_budget: number | null;
}

export interface ChannelUsage {
  since: string;
  tokens: number;
  cost: number | null;
  budget: number | null;
  agents: AgentUsage[];
  prices_configured: boolean;
}

export interface AuditEvent {
  id: number;
  at: string;
  channel_id: number | null;
  task_id: string | null;
  actor_type: "human" | "agent" | "system";
  actor_id: number | null;
  actor_name: string;
  action: string;
  detail: Record<string, unknown>;
}

export interface Artifact {
  path: string;
  content: string;
}

export interface ArtifactReference {
  task_id: string;
  path: string;
  size: number;
}

export interface LiveApp {
  id: string;
  title: string;
  port: number;
  path: string;
  agent_id: number;
  channel_id: number;
  status: "running" | "starting" | "paused" | "unavailable";
}

export interface AppPreviewSession {
  id: string;
  url: string;
  expires_at: string;
}

export interface TaskDetail extends Task {
  result: string;
  artifacts: Artifact[];
}

export interface Schedule {
  id: number;
  agent_id: number;
  prompt: string;
  when: string;
  next_run_at: string | null;
  last_run_at: string | null;
}

export interface Memory {
  key: string;
  content: string;
  updated_at: string;
}

export interface ComputerControl {
  user_name: string;
  until: string;
  mine: boolean;
}

export interface ComputerState {
  shared?: boolean;
  provider?: "e2b" | "tenki";
  configured: boolean;
  status: string;
  sandbox_id: string | null;
  image: string;
  width: number;
  height: number;
  last_action: string;
  steps: ComputerStep[];
  url: string;
  network_warning: string;
  observed_at: string | null;
  updated_at: string | null;
  error: string | null;
  dashboard_url: string;
  control: ComputerControl | null;
}

export interface LiveView {
  url: string;
  expires_at: string;
  control: ComputerControl | null;
}

export interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
}

export interface Role {
  id: string;
  label: string;
  description: string;
  instructions: string;
}

export interface Model {
  id: string;
  label: string;
  default?: boolean;
}

export interface WorkspaceDocument {
  id: string;
  channel_id: number;
  name: string;
  media_type: string;
  size: number;
  status: "staged" | "queued" | "processing" | "ready" | "error" | "deleting" | "deleted";
  error: string;
  uploaded_by: number;
  created_at: string;
  updated_at: string;
  preview?: string;
}

export interface BandStatus {
  configured: boolean;
  api?: "agent" | "human";
  online: boolean;
  personal?: boolean;
  owner_name: string | null;
  owner_handle: string | null;
  error: string | null;
}

export interface Bootstrap {
  me: User;
  users: User[];
  channels: Channel[];
  agents: Agent[];
  roles: Role[];
  workspace_roles: Record<string, WorkspaceRole>;
  models: Model[];
  band: BandStatus;
  openai_configured: boolean;
  llm_configured?: boolean;
  /** Monotonic snapshot cursor. Events with an equal or lower cursor are already included. */
  cursor?: number;
  invitations?: ChannelInvitation[];
  contact_requests?: PeopleResponse["requests"];
  service?: ServiceStatus;
}

export interface Draft {
  task_id: string;
  channel_id: number;
  agent_id: number;
  content: string;
}

export type ServerEvent = (
  | { type: "document_removed"; channel_id: number; document_id: string }
  | { type: "provider_settings_changed" }
  | { type: "openai_settings"; models: Model[]; configured: boolean }
  | { type: "skills_changed" }
  | { type: "message"; message: Message }
  | { type: "message_updated"; message: Message }
  | ({ type: "draft" } & Draft)
  | { type: "draft_append"; task_id: string; channel_id: number; agent_id: number; offset: number; text: string }
  | { type: "channel"; channel: Channel }
  | { type: "user"; user: User }
  | { type: "presence"; user_id: number; online: boolean }
  | { type: "agent"; agent: Agent }
  | { type: "agent_deleted"; agent_id: number }
  | { type: "agent_status"; agent_id: number; status: AgentStatus }
  | { type: "typing"; channel_id: number; agent_id: number; active: boolean }
  | { type: "band_status"; band: BandStatus }
  | { type: "channel_removed"; channel_id: number }
  | { type: "task_activity"; task: TaskActivity }
  | { type: "workspace_role"; user_id: number; role: WorkspaceRole }
  | { type: "budget"; scope: "agent" | "channel"; scope_id: number; daily_tokens: number | null }
  | { type: "people_changed" }
) & { cursor?: number };
