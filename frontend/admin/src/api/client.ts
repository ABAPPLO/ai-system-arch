/**
 * Admin API client —— 统一 fetch wrapper（Bearer JWT 鉴权版）。
 *
 * 钉钉 SSO 登录后存 access/refresh JWT；401 自动 refresh 一次，用新 token 重试；
 * 仅当无 refresh token、refresh 失败、或重试仍 401（真鉴权失效）时才清登录态跳登录。
 * 重试遇到 400/500 等业务错误按正常响应抛 ApiError，不强制登出。
 * 机器访问（脚本/API Key）仍可直接带 X-API-Key 头（本 client 仅服务浏览器 SSO 态）。
 */

const TOKEN_STORAGE = 'apihub_admin_token';
const REFRESH_STORAGE = 'apihub_admin_refresh';
const USER_STORAGE = 'apihub_admin_user';

export interface AuthState {
  token: string;
  user: { id: string; name: string; isPlatformAdmin: boolean; tenantId: string };
}

export function getAuth(): AuthState | null {
  const token = localStorage.getItem(TOKEN_STORAGE);
  const userJson = localStorage.getItem(USER_STORAGE);
  if (!token || !userJson) return null;
  try {
    return { token, user: JSON.parse(userJson) };
  } catch {
    return null;
  }
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_STORAGE);
}

export function setTokens(
  token: string,
  refreshToken: string,
  user: AuthState['user'],
): void {
  localStorage.setItem(TOKEN_STORAGE, token);
  localStorage.setItem(REFRESH_STORAGE, refreshToken);
  localStorage.setItem(USER_STORAGE, JSON.stringify(user));
}

export function clearAuth(): void {
  localStorage.removeItem(TOKEN_STORAGE);
  localStorage.removeItem(REFRESH_STORAGE);
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
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  skipAuth?: boolean;
  query?: Record<string, string | number | undefined | null>;
}

async function parseResponse<T>(resp: Response): Promise<T> {
  const ct = resp.headers.get('content-type') || '';
  let payload: unknown;
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

async function tryRefresh(refreshToken: string): Promise<string | null> {
  /** 用 refresh token 换新 access token；成功则落盘并返回新 token，失败/拒绝返回 null。 */
  try {
    const rr = await fetch('/api/auth/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!rr.ok) return null;
    const data = await rr.json();
    const auth = getAuth();
    setTokens(
      data.access_token,
      data.refresh_token,
      auth?.user || { id: '', name: '', isPlatformAdmin: false, tenantId: 'platform' },
    );
    return data.access_token as string;
  } catch {
    return null;
  }
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

  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (!skipAuth) {
    const auth = getAuth();
    if (auth) headers['Authorization'] = 'Bearer ' + auth.token;
  }

  const resp = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (resp.status === 401 && !skipAuth) {
    // 401 → 尝试用 refresh token 续期后重试一次。
    const rt = getRefreshToken();
    const newToken = rt ? await tryRefresh(rt) : null;
    if (newToken) {
      headers['Authorization'] = 'Bearer ' + newToken;
      const retry = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
      // retry 仍 401 → 新 token 也无效，真鉴权失效 → 登出；
      // retry 非 401（含 400/500 等业务错）→ 按正常响应返回，不登出。
      if (retry.status !== 401) {
        return parseResponse<T>(retry);
      }
    }
    // 无 refresh token / refresh 失败 / retry 仍 401 → 登出。
    clearAuth();
    window.location.href = '/login';
    throw new ApiError(401, 10002, 'Unauthorized');
  }

  return parseResponse<T>(resp);
}

export const api = {
  get: <T>(path: string, query?: RequestOptions['query']) =>
    request<T>(path, { method: 'GET', query }),
  post: <T>(path: string, body?: unknown, opts?: { skipAuth?: boolean }) =>
    request<T>(path, { method: 'POST', body, ...opts }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

/**
 * 下载 CSV（带 Bearer）。fetch → blob → 触发浏览器下载。
 */
export async function downloadCsv(url: string, filename: string): Promise<void> {
  const headers: Record<string, string> = {};
  const auth = getAuth();
  if (auth) headers['Authorization'] = 'Bearer ' + auth.token;

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
