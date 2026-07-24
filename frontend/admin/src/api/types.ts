/**
 * 共享 TypeScript 类型 —— 对齐后端 Pydantic 模型。
 *
 * 后端权威：services/services/{admin,retry,api-registry,trace}/src/.../models.py
 * 这里只列前端用到的字段（detail/full 字段按需复制）。
 */

// ===== admin-bff =====

export interface AuditListItem {
  id: number;
  tenant_id: string;
  actor_type: string;
  actor_id: string | null;
  actor_name: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  resource_name: string | null;
  created_at: string;
}

export interface AuditDetail extends AuditListItem {
  actor_ip: string | null;
  auth_method: string | null;
  env: string | null;
  detail: Record<string, unknown>;
  user_agent: string | null;
  request_id: string | null;
  trace_id: string | null;
}

export interface AuditStats {
  total: number;
  top_actions: Record<string, unknown>[];
  top_actors: Record<string, unknown>[];
  by_day: Record<string, unknown>[];
}

export interface DashboardResponse {
  tenants: Record<string, unknown>;
  audit_today: number;
  audit_7d: number;
  top_recent_events: AuditListItem[];
}

// ===== retry-svc =====

export type RetryStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'dead'
  | 'ignored';

export type BackoffPolicy = 'exponential' | 'fixed' | 'linear';

export interface RetryTaskRow {
  id: number;
  tenant_id: string;
  trace_id: string;
  task_instance_id: string | null;
  api_id: string;
  app_id: string;
  max_attempts: number;
  retry_count: number;
  next_retry_at: string | null;
  backoff_policy: BackoffPolicy;
  backoff_base_ms: number;
  status: RetryStatus;
  env: string;
  last_error_code: string | null;
  last_error_msg: string | null;
  last_failed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RetryAttemptRow {
  id: number;
  attempt_no: number;
  response_status: number | null;
  error_code: string | null;
  error_msg: string | null;
  latency_ms: number | null;
  attempted_at: string;
}

export interface RetryTaskDetail extends RetryTaskRow {
  attempts: RetryAttemptRow[];
  original_request: Record<string, unknown>;
}

export interface RetryStats {
  total: number;
  pending: number;
  running: number;
  dead: number;
  ignored: number;
  succeeded: number;
  success_rate: number;
  by_error_code: Record<string, number>;
}

// ===== api-registry (change_request) =====

export type ChangeRequestStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'applied'
  | 'cancelled';

export type ChangeType =
  | 'create'
  | 'update'
  | 'publish'
  | 'deprecate'
  | 'retire';

export type TargetEnv = 'dev' | 'staging' | 'prod';

export interface ChangeRequest {
  id: number;
  tenant_id: string;
  api_id: string;
  target_version: string;
  change_type: ChangeType;
  target_env: TargetEnv;
  proposed_config: Record<string, unknown>;
  current_config?: Record<string, unknown> | null;
  diff_summary?: string | null;
  status: ChangeRequestStatus;
  dingtalk_approval_id: string | null;
  submitted_by: string;
  submitted_at: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  review_comment?: string | null;
  applied_at?: string | null;
}

// ===== api-registry (api 元数据) =====

export interface ApiListItem {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  category: string;
  base_path: string;
  tags: string[] | null;
  visibility?: string;
  status: string; // draft / reviewing / published / deprecated / retired
  created_at: string;
  updated_at: string;
}

/** GET /v1/apis/{id} 返回同表全字段，结构与列表项一致。 */
export type ApiDetail = ApiListItem;

// ===== trace-svc =====

export type CallStatus = 'all' | 'success' | 'failed' | 'timeout';

export interface CallListItem {
  trace_id: string;
  api_id: string;
  api_path: string;
  api_method: string;
  api_version: string;
  app_id: string;
  app_name: string | null;
  caller_ip: string | null;
  http_status: number;
  is_success: boolean;
  is_timeout: boolean;
  latency_ms: number;
  error_type: string | null;
  error_msg: string | null;
  ts: string;
}

export interface CallDetail extends CallListItem {
  parent_trace_id: string | null;
  span_id: string | null;
  api_mode: string | null;
  env: string | null;
  gateway_node: string | null;
  req_id: string | null;
  req_size: number | null;
  resp_size: number | null;
  gateway_latency_ms: number | null;
  backend_latency_ms: number | null;
  is_streaming: boolean;
  token_prompt: number | null;
  token_completion: number | null;
  token_total: number | null;
  ai_model: string | null;
  is_retry: boolean;
  retry_no: number | null;
  task_id: string | null;
}

export interface CallStats {
  total: number;
  success_count: number;
  failed_count: number;
  timeout_count: number;
  success_rate: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  p99_latency_ms: number;
  avg_latency_ms: number;
  qps: number;
  top_apis: Record<string, unknown>[];
  by_hour: Record<string, unknown>[];
}

// ===== tenant-svc =====

export interface TenantListItem {
  id: string;
  name: string;
  slug: string;
  type: string; // internal / external / system
  status: string; // active / suspended / closed
  tier: string; // free / standard / premium
  parent_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type TenantDetail = TenantListItem;

export interface TenantCreateBody {
  id: string;
  name: string;
  slug: string;
  type?: string;
  parent_id?: string | null;
  tier?: string;
  metadata?: Record<string, unknown>;
}

export interface TenantUpdateBody {
  name?: string;
  slug?: string;
  tier?: string;
  metadata?: Record<string, unknown>;
}

export interface TenantMember {
  id: string;
  tenant_id: string;
  user_id: string;
  role: string; // owner / admin / developer / viewer
  created_at: string;
}

export interface TenantQuota {
  day_limit: number;
  rate_limit: Record<string, unknown>;
}

export interface TenantUsage {
  tenant_id: string;
  day_used: number;
  day_limit: number;
  remaining: number;
}

// ===== api-registry (api_version) =====

export interface ApiVersion {
  id: string;
  api_id: string;
  version: string;
  backend_type: string; // http / async_task / workflow / ai_model
  backend_url: string;
  method: string;
  path: string;
  status: string; // draft / reviewing / published / deprecated / retired
  ai_model: string | null;
  ai_streaming: boolean;
  created_at: string;
  published_at: string | null;
}

export interface ApiCreateBody {
  name: string;
  description?: string | null;
  category: string;
  base_path: string;
  tags?: string[];
}

export interface ApiVersionCreateBody {
  api_id: string;
  version: string; // v1, v2, ...
  backend_type?: string;
  backend_url: string;
  method?: string;
  path: string;
  request_schema?: Record<string, unknown> | null;
  response_schema?: Record<string, unknown> | null;
  ai_model?: string | null;
  ai_streaming?: boolean;
}
