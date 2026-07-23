/**
 * API client —— 统一 fetch wrapper。
 *
 * - 自动注入 X-API-Key header（从 localStorage 取）
 * - 401 自动跳登录
 * - 业务错误码透传（{ success, code, message } 结构，详见 apihub_core.errors）
 */

const API_KEY_STORAGE = 'apihub_admin_api_key';
const USER_STORAGE = 'apihub_admin_user';

export interface AuthState {
  apiKey: string;
  user: { id: string; name: string; isPlatformAdmin: boolean; tenantId: string };
}

export function getAuth(): AuthState | null {
  const apiKey = localStorage.getItem(API_KEY_STORAGE);
  const userJson = localStorage.getItem(USER_STORAGE);
  if (!apiKey || !userJson) return null;
  try {
    return { apiKey, user: JSON.parse(userJson) };
  } catch {
    return null;
  }
}

export function setAuth(apiKey: string, user: AuthState['user']): void {
  localStorage.setItem(API_KEY_STORAGE, apiKey);
  localStorage.setItem(USER_STORAGE, JSON.stringify(user));
}

export function clearAuth(): void {
  localStorage.removeItem(API_KEY_STORAGE);
  localStorage.removeItem(USER_STORAGE);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: number,
    message: string,
  ) {
    super(message);
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  // 跳过 X-API-Key 注入（仅 /login 用）
  skipAuth?: boolean;
  query?: Record<string, string | number | undefined | null>;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipAuth, query } = opts;

  let url = path;
  if (query) {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
    }
    const qs = sp.toString();
    if (qs) url += (path.includes('?') ? '&' : '?') + qs;
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (!skipAuth) {
    const auth = getAuth();
    if (auth) headers['X-API-Key'] = auth.apiKey;
  }

  const resp = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (resp.status === 401 && !skipAuth) {
    clearAuth();
    window.location.href = '/login';
    throw new ApiError(401, 10002, 'Unauthorized');
  }

  let payload: unknown = null;
  const ct = resp.headers.get('content-type') || '';
  if (ct.includes('application/json')) {
    payload = await resp.json();
  } else {
    payload = await resp.text();
  }

  if (!resp.ok) {
    const errBody = (payload && typeof payload === 'object'
      ? (payload as { message?: string; code?: number })
      : {}) as { message?: string; code?: number };
    throw new ApiError(
      resp.status,
      errBody.code ?? resp.status,
      errBody.message || `HTTP ${resp.status}`,
    );
  }

  return payload as T;
}

export const api = {
  get: <T>(path: string, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'GET', query }),
  post: <T>(path: string, body?: unknown, opts?: { skipAuth?: boolean }) =>
    request<T>(path, { method: 'POST', body, ...opts }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

/**
 * 下载 CSV（带 X-API-Key）。fetch → blob → 触发浏览器下载。
 */
export async function downloadCsv(
  url: string,
  filename: string,
): Promise<void> {
  const headers: Record<string, string> = {};
  const auth = getAuth();
  if (auth) headers['X-API-Key'] = auth.apiKey;

  const resp = await fetch(url, { headers });
  if (!resp.ok) {
    throw new ApiError(resp.status, resp.status, `导出失败：HTTP ${resp.status}`);
  }
  const blob = await resp.blob();
  const objUrl = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = objUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(objUrl);
}
