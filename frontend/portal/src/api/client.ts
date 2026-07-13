/**
 * Portal API client —— 统一 fetch wrapper（JWT 鉴权版）。
 *
 * 支持 refresh token 自动续期：401 时自动调 /v1/portal/auth/refresh，
 * 成功则重试原请求，失败则跳登录。
 */

const TOKEN_STORAGE = 'apihub_portal_token';
const REFRESH_STORAGE = 'apihub_portal_refresh';
const USER_STORAGE = 'apihub_portal_user';

export interface AuthState {
  token: string;
  user: { id: string; name: string; tenantId: string };
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

export function setTokens(token: string, refreshToken: string, user: AuthState['user']): void {
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
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  body?: unknown;
  // 跳过 Authorization 注入（注册/登录用）
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
    if (auth) headers['Authorization'] = 'Bearer ' + auth.token;
  }

  const resp = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (resp.status === 401 && !skipAuth) {
    // Try refresh token
    const rt = getRefreshToken();
    if (rt) {
      try {
        const rr = await fetch('/v1/portal/auth/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: rt }),
        });
        if (rr.ok) {
          const data = await rr.json();
          const auth = getAuth();
          setTokens(data.access_token, data.refresh_token, auth?.user || { id: '', name: '', tenantId: '' });
          headers['Authorization'] = 'Bearer ' + data.access_token;
          const retry = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
          if (retry.ok) {
            const ct = retry.headers.get('content-type') || '';
            return ct.includes('application/json') ? await retry.json() : (await retry.text()) as T;
          }
          // retry also 401 — fall through to logout
        }
      } catch { /* fall through to logout */ }
    }
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
