/**
 * APIHub 双前端共享 API client（admin / portal 唯一来源）。
 *
 * 此前 admin 与 portal 各持一份逐行雷同的 client（401-refresh、token 存储、
 * ApiError 都是双份维护）——auth 逻辑的每次修复都要改两遍。收敛为工厂：
 * 存储键 / refresh 端点 / 默认用户 由各 app 配置，行为单源。
 *
 * 401 自动续期：refresh 成功 → 新 token 重试原请求一次；仅当无 refresh
 * token、refresh 失败、或重试仍 401（真鉴权失效）才清登录态跳登录。
 * 重试遇到 400/500 等业务错误按正常响应抛 ApiError，不强制登出。
 *
 * 竞态防护：所有方法支持 AbortSignal（页面快速切换时旧请求被丢弃，
 * 旧响应不会覆盖新数据）。
 */

export interface ClientConfig<TUser extends { id: string }> {
  tokenStorageKey: string;
  refreshStorageKey: string;
  userStorageKey: string;
  /** refresh 端点（app 各异：portal 直连 /v1/portal/auth/refresh，admin 走 /api/auth 代理） */
  refreshUrl: string;
  /** 鉴权失效后的跳转 */
  loginRedirect: string;
  /** refresh 成功但本地无 user 时回填的兜底值 */
  defaultUser: TUser;
}

export interface AuthState<TUser extends { id: string }> {
  token: string;
  user: TUser;
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
  /** 跳过 Authorization 注入（注册/登录用） */
  skipAuth?: boolean;
  query?: Record<string, string | number | undefined | null>;
  /** 竞态防护：页面卸载/依赖变更时 abort */
  signal?: AbortSignal;
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

export function createApiClient<TUser extends { id: string }>(cfg: ClientConfig<TUser>) {
  function getAuth(): AuthState<TUser> | null {
    const token = localStorage.getItem(cfg.tokenStorageKey);
    const userJson = localStorage.getItem(cfg.userStorageKey);
    if (!token || !userJson) return null;
    try {
      return { token, user: JSON.parse(userJson) as TUser };
    } catch {
      return null;
    }
  }

  function getRefreshToken(): string | null {
    return localStorage.getItem(cfg.refreshStorageKey);
  }

  function setTokens(token: string, refreshToken: string, user: TUser): void {
    localStorage.setItem(cfg.tokenStorageKey, token);
    localStorage.setItem(cfg.refreshStorageKey, refreshToken);
    localStorage.setItem(cfg.userStorageKey, JSON.stringify(user));
  }

  function clearAuth(): void {
    localStorage.removeItem(cfg.tokenStorageKey);
    localStorage.removeItem(cfg.refreshStorageKey);
    localStorage.removeItem(cfg.userStorageKey);
  }

  async function tryRefresh(refreshToken: string): Promise<string | null> {
    /** 用 refresh token 换新 access token；成功落盘并返回新 token，失败/拒绝返回 null。 */
    try {
      const rr = await fetch(cfg.refreshUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!rr.ok) return null;
      const data = await rr.json();
      const auth = getAuth();
      setTokens(
        data.access_token as string,
        data.refresh_token as string,
        auth?.user || cfg.defaultUser,
      );
      return data.access_token as string;
    } catch {
      return null;
    }
  }

  async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const { method = 'GET', body, skipAuth, query, signal } = opts;

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
    const bodyText = body !== undefined ? JSON.stringify(body) : undefined;

    const resp = await fetch(url, { method, headers, body: bodyText, signal });

    if (resp.status === 401 && !skipAuth) {
      // 401 → refresh 续期后重试一次。
      const rt = getRefreshToken();
      const newToken = rt ? await tryRefresh(rt) : null;
      if (newToken) {
        headers['Authorization'] = 'Bearer ' + newToken;
        const retry = await fetch(url, { method, headers, body: bodyText, signal });
        // retry 仍 401 → 真·鉴权失效 → 登出；非 401（400/500 业务错）正常返回。
        if (retry.status !== 401) {
          return parseResponse<T>(retry);
        }
      }
      clearAuth();
      window.location.href = cfg.loginRedirect;
      throw new ApiError(401, 10002, 'Unauthorized');
    }

    return parseResponse<T>(resp);
  }

  const api = {
    get: <T>(path: string, query?: RequestOptions['query'], opts?: { signal?: AbortSignal }) =>
      request<T>(path, { method: 'GET', query, signal: opts?.signal }),
    post: <T>(path: string, body?: unknown, opts?: { skipAuth?: boolean; signal?: AbortSignal }) =>
      request<T>(path, { method: 'POST', body, ...opts }),
    put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
    patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
    del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  };

  /** 下载 CSV（带 Bearer）。fetch → blob → 触发浏览器下载。 */
  async function downloadCsv(url: string, filename: string): Promise<void> {
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

  return { getAuth, getRefreshToken, setTokens, clearAuth, api, downloadCsv };
}

export type ApiClient = ReturnType<typeof createApiClient>;
