/**
 * Типизированный клиент API Agent Civilization.
 *
 * Контракт полностью описан OpenAPI-схемой backend (/openapi.json),
 * поэтому клиента можно сгенерировать на любом языке. Здесь — ручная
 * реализация на TypeScript без внешних зависимостей.
 */

export class ApiError extends Error {
  code: string
  status: number
  details: Record<string, unknown>
  requestId: string | null

  constructor(
    message: string,
    options: { code: string; status: number; details?: Record<string, unknown>; requestId?: string | null },
  ) {
    super(message)
    this.name = 'ApiError'
    this.code = options.code
    this.status = options.status
    this.details = options.details ?? {}
    this.requestId = options.requestId ?? null
  }
}

const ACCESS_TOKEN_KEY = 'ac.access_token'
const REFRESH_TOKEN_KEY = 'ac.refresh_token'

export const tokenStore = {
  get access() {
    return localStorage.getItem(ACCESS_TOKEN_KEY)
  },
  get refresh() {
    return localStorage.getItem(REFRESH_TOKEN_KEY)
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_TOKEN_KEY, access)
    localStorage.setItem(REFRESH_TOKEN_KEY, refresh)
  },
  setAccess(access: string) {
    localStorage.setItem(ACCESS_TOKEN_KEY, access)
  },
  clear() {
    localStorage.removeItem(ACCESS_TOKEN_KEY)
    localStorage.removeItem(REFRESH_TOKEN_KEY)
  },
}

export interface UserSummary {
  id: string
  display_name: string
}

export interface UserPublic extends UserSummary {
  email: string
  role: 'member' | 'developer' | 'admin'
  is_active: boolean
  email_verified: boolean
  created_at: string
  last_login_at: string | null
}

export type AgentStatus = 'draft' | 'published' | 'suspended' | 'archived'
export type EntrypointType = 'http' | 'python' | 'docker' | 'mcp'

export interface AgentCapability {
  capability_key: string
  declared_level: number
  verified_level: number | null
  verified_at?: string | null
  note?: string | null
}

export interface AgentVersion {
  id: string
  version: number
  changelog: string
  entrypoint_type: EntrypointType
  entrypoint_config: Record<string, unknown>
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  required_capabilities: string[]
  resource_limits: Record<string, unknown>
  published_at: string | null
  deprecated_at: string | null
  created_at: string
}

export interface AgentListItem {
  id: string
  slug: string
  name: string
  summary: string
  status: AgentStatus
  category: string
  tags: string[]
  accent_color: string
  current_version: number
  is_verified: boolean
  reputation_score: number
  completed_tasks: number
  failed_tasks: number
  owner: UserSummary
  capabilities: AgentCapability[]
  published_at: string | null
  created_at: string
}

export interface Agent extends Omit<AgentListItem, 'tags'> {
  description: string
  tags: string[]
  homepage_url: string | null
  repository_url: string | null
  versions: AgentVersion[]
  updated_at: string
}

export interface PageMeta {
  page: number
  page_size: number
  total: number
  pages: number
}

export interface Page<T> {
  items: T[]
  meta: PageMeta
}

export interface Capability {
  id: string
  key: string
  name: string
  description: string
  category: string
  is_active: boolean
}

export type NodeStatus = 'pending' | 'online' | 'degraded' | 'offline' | 'revoked'
export type NodeHealth = 'unknown' | 'healthy' | 'unhealthy'

export interface InfrastructureNode {
  id: string
  node_key: string
  name: string
  kind: 'cloud' | 'community' | 'edge'
  status: NodeStatus
  health: NodeHealth
  region: string
  endpoint_url: string | null
  cpu_cores: number
  memory_mb: number
  max_concurrency: number
  running_tasks: number
  limit_cpu_seconds: number
  limit_wall_seconds: number
  limit_memory_mb: number
  allow_network: boolean
  capabilities: string[]
  labels: Record<string, unknown>
  reputation_score: number
  trust_level: number
  last_seen_at: string | null
  verified_at: string | null
  revoked_at: string | null
  revoked_reason: string | null
  owner_id: string
  created_at: string
}

export type TaskStatus =
  | 'queued'
  | 'leased'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'expired'

export interface Task {
  id: string
  agent_id: string
  agent_version_id: string | null
  node_id: string | null
  status: TaskStatus
  priority: number
  attempts: number
  max_attempts: number
  input_payload: Record<string, unknown>
  output_payload: Record<string, unknown>
  error: Record<string, unknown> | null
  result_hash: string | null
  cost_units: number
  lease_expires_at: string | null
  deadline_at: string | null
  started_at: string | null
  finished_at: string | null
  trace_id: string | null
  created_at: string
  updated_at: string
}

export interface TaskEvent {
  id: string
  sequence: number
  event_type: string
  actor: string
  message: string
  data: Record<string, unknown>
  created_at: string
}

export interface PlatformStats {
  users: number
  agents: { total: number; by_status: Record<string, number>; published: number }
  tasks: { total: number; by_status: Record<string, number>; oldest_queued_at: string | null }
  nodes: { total: number; by_status: Record<string, number>; online_capacity: number }
}

export interface NodeCredentials {
  node_key: string
  node_secret: string
  warning: string
}

export interface NodeRegistration {
  node: InfrastructureNode
  credentials: NodeCredentials
}

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

interface RequestOptions {
  method?: string
  body?: unknown
  auth?: boolean
  signal?: AbortSignal
  retryOn401?: boolean
}

let refreshPromise: Promise<boolean> | null = null

async function tryRefresh(): Promise<boolean> {
  const refresh = tokenStore.refresh
  if (!refresh) return false
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const response = await fetch(`${BASE}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refresh }),
        })
        if (!response.ok) {
          tokenStore.clear()
          return false
        }
        const data = (await response.json()) as { access_token: string; refresh_token: string }
        tokenStore.set(data.access_token, data.refresh_token)
        return true
      } catch {
        return false
      } finally {
        refreshPromise = null
      }
    })()
  }
  return refreshPromise
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, auth = false, signal, retryOn401 = true } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth) {
    const token = tokenStore.access
    if (token) headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })

  if (response.status === 401 && auth && retryOn401) {
    const refreshed = await tryRefresh()
    if (refreshed) {
      return request<T>(path, { ...options, retryOn401: false })
    }
  }

  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  const payload = text ? (JSON.parse(text) as unknown) : null

  if (!response.ok) {
    const error = (payload as { error?: { message?: string; code?: string; details?: Record<string, unknown>; request_id?: string } } | null)?.error
    throw new ApiError(error?.message ?? `HTTP ${response.status}`, {
      code: error?.code ?? `http_${response.status}`,
      status: response.status,
      details: error?.details,
      requestId: error?.request_id,
    })
  }

  return payload as T
}


// ── Коммуникация агентов ────────────────────────────────────────────────
export type MessageKind = 'request' | 'response' | 'event' | 'error'
export type DeliveryStrategy = 'http' | 'sandbox' | 'mcp' | 'inbox'
export type MessageStatus = 'pending' | 'processing' | 'delivered' | 'failed' | 'expired'
export type ConversationStatus = 'open' | 'closed' | 'archived'

export interface AgentMessage {
  id: string
  conversation_id: string
  sender_agent_id: string | null
  recipient_agent_id: string | null
  kind: MessageKind
  strategy: DeliveryStrategy
  status: MessageStatus
  correlation_id: string | null
  payload: Record<string, unknown>
  response_payload: Record<string, unknown>
  error: Record<string, unknown> | null
  error_message: string | null
  attempts: number
  max_attempts: number
  latency_ms: number | null
  delivered_at: string | null
  created_at: string
}

export interface ConversationSummary {
  id: string
  subject: string
  status: ConversationStatus
  participant_ids: string[]
  task_id: string | null
  message_count: number
  last_message_at: string | null
  created_at: string
}

export interface ConversationDetail extends ConversationSummary {
  messages: AgentMessage[]
}

export interface MessageSendResult {
  message: AgentMessage
  is_duplicate: boolean
  conversation_id: string
}

export interface CapabilityMatch {
  agent_id: string
  slug: string
  name: string
  score: number
  matched_capabilities: string[]
  missing_capabilities: string[]
  reputation_score: number
  completed_tasks: number
}

export interface DiscoveryResponse {
  required_capabilities: string[]
  matches: CapabilityMatch[]
}

export interface McpServer {
  agent_id: string
  name: string
  server_url: string
  transport: 'http' | 'stdio'
  tools: Array<{ name: string; description?: string }>
  has_auth_token: boolean
  protocol_version: string | null
  tools_synced_at: string | null
}


// ── Оркестрация (планы) ─────────────────────────────────────────────────
export type PlanStatus = 'draft' | 'planned' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type StepStatus =
  | 'pending'
  | 'ready'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'blocked'

export interface PlanStep {
  id: string
  key: string
  name: string
  position: number
  required_capabilities: string[]
  depends_on: string[]
  input_payload: Record<string, unknown>
  resolved_input: Record<string, unknown>
  output_payload: Record<string, unknown>
  error: Record<string, unknown> | null
  status: StepStatus
  agent_id: string | null
  task_id: string | null
  match_score: number | null
  attempts: number
  max_attempts: number
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  note: string | null
}

export interface PlanSummary {
  id: string
  title: string
  goal: Record<string, unknown>
  status: PlanStatus
  max_parallelism: number
  failure_policy: 'fail_plan' | 'continue_'
  step_timeout_seconds: number
  planner_agent_id: string | null
  planner_message_id: string | null
  error: Record<string, unknown> | null
  trace_id: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string | null
}

export interface PlanEvent {
  sequence: number
  event_type: string
  step_key: string | null
  actor: string
  message: string
  data: Record<string, unknown>
  created_at: string
}

export interface PlanDetail extends PlanSummary {
  steps: PlanStep[]
  events: PlanEvent[]
}

export interface PlanProgress {
  plan_id: string
  status: PlanStatus
  total: number
  by_status: Record<string, number>
  ready: number
  running: number
  finished: number
  percent: number
}

export interface PlanRunResult {
  plan_id: string
  status: PlanStatus
  launched: string[]
  completed: string[]
  failed: string[]
  blocked: string[]
  progress: PlanProgress
}

// ── Auth ────────────────────────────────────────────────────────────────
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: UserPublic
}

export const api = {
  async register(email: string, password: string, displayName: string): Promise<TokenPair> {
    const data = await request<TokenPair>('/auth/register', {
      method: 'POST',
      body: { email, password, display_name: displayName },
    })
    tokenStore.set(data.access_token, data.refresh_token)
    return data
  },

  async login(email: string, password: string): Promise<TokenPair> {
    const data = await request<TokenPair>('/auth/login', {
      method: 'POST',
      body: { email, password },
    })
    tokenStore.set(data.access_token, data.refresh_token)
    return data
  },

  async logout(): Promise<void> {
    try {
      await request('/auth/logout', {
        method: 'POST',
        auth: true,
        body: { refresh_token: tokenStore.refresh, all_sessions: false },
      })
    } finally {
      tokenStore.clear()
    }
  },

  me(signal?: AbortSignal): Promise<UserPublic> {
    return request<UserPublic>('/auth/me', { auth: true, signal })
  },

  myAgents(signal?: AbortSignal): Promise<AgentListItem[]> {
    return request<AgentListItem[]>('/auth/me/agents', { auth: true, signal })
  },

  // ── Agents ──────────────────────────────────────────────────────────
  agents(params: Record<string, string | number | boolean | string[] | undefined> = {}, signal?: AbortSignal) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      if (Array.isArray(value)) value.forEach((item) => search.append(key, item))
      else search.append(key, String(value))
    }
    const query = search.toString()
    return request<Page<AgentListItem>>(`/agents${query ? `?${query}` : ''}`, {
      auth: Boolean(tokenStore.access),
      signal,
    })
  },

  agent(id: string, signal?: AbortSignal): Promise<Agent> {
    return request<Agent>(`/agents/${id}`, { auth: Boolean(tokenStore.access), signal })
  },

  agentBySlug(slug: string, signal?: AbortSignal): Promise<Agent> {
    return request<Agent>(`/agents/by-slug/${slug}`, { auth: Boolean(tokenStore.access), signal })
  },

  createAgent(payload: Record<string, unknown>): Promise<Agent> {
    return request<Agent>('/agents', { method: 'POST', body: payload, auth: true })
  },

  updateAgent(id: string, payload: Record<string, unknown>): Promise<Agent> {
    return request<Agent>(`/agents/${id}`, { method: 'PATCH', body: payload, auth: true })
  },

  publishAgent(id: string, publish = true): Promise<Agent> {
    return request<Agent>(`/agents/${id}/publish`, { method: 'POST', body: { publish }, auth: true })
  },

  suspendAgent(id: string, reason: string): Promise<Agent> {
    return request<Agent>(`/agents/${id}/suspend`, { method: 'POST', body: { reason }, auth: true })
  },

  resumeAgent(id: string): Promise<Agent> {
    return request<Agent>(`/agents/${id}/resume`, { method: 'POST', body: {}, auth: true })
  },

  deleteAgent(id: string): Promise<void> {
    return request<void>(`/agents/${id}`, { method: 'DELETE', auth: true })
  },

  createVersion(id: string, payload: Record<string, unknown>): Promise<AgentVersion> {
    return request<AgentVersion>(`/agents/${id}/versions`, { method: 'POST', body: payload, auth: true })
  },

  capabilities(signal?: AbortSignal): Promise<Capability[]> {
    return request<Capability[]>('/capabilities', { signal })
  },

  // ── Nodes ───────────────────────────────────────────────────────────
  nodes(params: Record<string, string | number | boolean | undefined> = {}, signal?: AbortSignal) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined) continue
      search.append(key, String(value))
    }
    const query = search.toString()
    return request<Page<InfrastructureNode>>(`/nodes${query ? `?${query}` : ''}`, {
      auth: true,
      signal,
    })
  },

  registerNode(payload: Record<string, unknown>): Promise<NodeRegistration> {
    return request<NodeRegistration>('/nodes', { method: 'POST', body: payload, auth: true })
  },

  revokeNode(id: string, reason: string): Promise<InfrastructureNode> {
    return request<InfrastructureNode>(`/nodes/${id}/revoke?reason=${encodeURIComponent(reason)}`, {
      method: 'POST',
      auth: true,
    })
  },

  rotateNodeSecret(id: string): Promise<NodeCredentials> {
    return request<NodeCredentials>(`/nodes/${id}/rotate-secret`, { method: 'POST', auth: true })
  },

  nodeHeartbeats(id: string, signal?: AbortSignal) {
    return request<Array<{ id: string; sequence: number; running_tasks: number; cpu_load: number; memory_used_mb: number; health: NodeHealth; created_at: string }>>(
      `/nodes/${id}/heartbeats`,
      { auth: true, signal },
    )
  },

  // ── Tasks ───────────────────────────────────────────────────────────
  submitTask(payload: Record<string, unknown>): Promise<Task> {
    return request<Task>('/tasks', { method: 'POST', body: payload, auth: true })
  },

  tasks(params: Record<string, string | number | undefined> = {}, signal?: AbortSignal) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined) continue
      search.append(key, String(value))
    }
    const query = search.toString()
    return request<Page<Task>>(`/tasks${query ? `?${query}` : ''}`, { auth: true, signal })
  },

  task(id: string, signal?: AbortSignal): Promise<Task> {
    return request<Task>(`/tasks/${id}`, { auth: true, signal })
  },

  taskEvents(id: string, signal?: AbortSignal): Promise<TaskEvent[]> {
    return request<TaskEvent[]>(`/tasks/${id}/events`, { auth: true, signal })
  },

  cancelTask(id: string, reason: string): Promise<Task> {
    return request<Task>(`/tasks/${id}/cancel`, { method: 'POST', body: { reason }, auth: true })
  },


  // ── Коммуникация агентов ────────────────────────────────────────────
  conversations(params: Record<string, string | number | undefined> = {}, signal?: AbortSignal) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined) continue
      search.append(key, String(value))
    }
    const query = search.toString()
    return request<Page<ConversationSummary>>(`/conversations${query ? `?${query}` : ''}`, {
      auth: true,
      signal,
    })
  },

  conversation(id: string, signal?: AbortSignal): Promise<ConversationDetail> {
    return request<ConversationDetail>(`/conversations/${id}`, { auth: true, signal })
  },

  createConversation(payload: Record<string, unknown>): Promise<ConversationSummary> {
    return request<ConversationSummary>('/conversations', { method: 'POST', body: payload, auth: true })
  },

  closeConversation(id: string): Promise<ConversationSummary> {
    return request<ConversationSummary>(`/conversations/${id}/close`, { method: 'POST', auth: true })
  },

  sendMessage(agentId: string, payload: Record<string, unknown>): Promise<MessageSendResult> {
    return request<MessageSendResult>(`/agents/${agentId}/messages`, {
      method: 'POST',
      body: payload,
      auth: true,
    })
  },

  agentInbox(agentId: string, signal?: AbortSignal): Promise<AgentMessage[]> {
    return request<AgentMessage[]>(`/agents/${agentId}/inbox`, { auth: true, signal })
  },

  discovery(capabilities: string[], limit = 5, signal?: AbortSignal): Promise<DiscoveryResponse> {
    const search = new URLSearchParams()
    for (const capability of capabilities) search.append('capabilities', capability)
    search.append('limit', String(limit))
    return request<DiscoveryResponse>(`/discovery/agents?${search.toString()}`, { signal })
  },

  mcpServer(agentId: string, signal?: AbortSignal): Promise<McpServer> {
    return request<McpServer>(`/agents/${agentId}/mcp`, { auth: true, signal })
  },

  registerMcpServer(agentId: string, payload: Record<string, unknown>): Promise<McpServer> {
    return request<McpServer>(`/agents/${agentId}/mcp`, { method: 'PUT', body: payload, auth: true })
  },

  callMcpTool(agentId: string, payload: Record<string, unknown>): Promise<MessageSendResult> {
    return request<MessageSendResult>(`/agents/${agentId}/mcp/call`, {
      method: 'POST',
      body: payload,
      auth: true,
    })
  },

  messageStats(signal?: AbortSignal): Promise<Record<string, number>> {
    return request<Record<string, number>>('/messages/stats', { signal })
  },

  // ── Оркестрация ─────────────────────────────────────────────────────
  plans(params: Record<string, string | number | undefined> = {}, signal?: AbortSignal) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined) continue
      search.append(key, String(value))
    }
    const query = search.toString()
    return request<Page<PlanSummary>>(`/plans${query ? `?${query}` : ''}`, {
      auth: true,
      signal,
    })
  },

  plan(id: string, signal?: AbortSignal): Promise<PlanDetail> {
    return request<PlanDetail>(`/plans/${id}`, { auth: true, signal })
  },

  createPlan(payload: Record<string, unknown>): Promise<PlanDetail> {
    return request<PlanDetail>('/plans', { method: 'POST', body: payload, auth: true })
  },

  runPlan(id: string): Promise<PlanRunResult> {
    return request<PlanRunResult>(`/plans/${id}/run`, { method: 'POST', auth: true })
  },

  retryPlanStep(planId: string, stepKey: string): Promise<{ step: PlanStep }> {
    return request<{ step: PlanStep }>(`/plans/${planId}/steps/${stepKey}/retry`, {
      method: 'POST',
      auth: true,
    })
  },

  cancelPlan(id: string, reason = ''): Promise<PlanSummary> {
    return request<PlanSummary>(`/plans/${id}/cancel?reason=${encodeURIComponent(reason)}`, {
      method: 'POST',
      auth: true,
    })
  },

  planStats(signal?: AbortSignal): Promise<{
    plans: Record<string, number>
    plans_total: number
    steps: Record<string, number>
  }> {
    return request('/plans/stats', { auth: true, signal })
  },

  // ── Platform ────────────────────────────────────────────────────────
  stats(signal?: AbortSignal): Promise<PlatformStats> {
    return request<PlatformStats>('/platform/stats', { signal })
  },

  health(signal?: AbortSignal) {
    return request<{ status: string; version: string; environment: string; checks: Record<string, string> }>(
      '/health/ready',
      { signal },
    )
  },
}
