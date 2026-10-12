/**
 * Portal API client —— 共享工厂（@apihub/shared）的 portal 配置。
 *
 * JWT 鉴权 + 401 自动 refresh 的实现在共享包单源维护（admin/portal 一份逻辑），
 * 这里只声明 portal 的差异：存储键、refresh 端点、默认用户、登录跳转。
 */

import { createApiClient } from '@apihub/shared';

export interface PortalUser {
  id: string;
  name: string;
  tenantId: string;
}

export interface AuthState {
  token: string;
  user: PortalUser;
}

const client = createApiClient<PortalUser>({
  tokenStorageKey: 'apihub_portal_token',
  refreshStorageKey: 'apihub_portal_refresh',
  userStorageKey: 'apihub_portal_user',
  refreshUrl: '/v1/portal/auth/refresh',
  loginRedirect: '/login',
  defaultUser: { id: '', name: '', tenantId: '' },
});

export const getAuth = client.getAuth;
export const getRefreshToken = client.getRefreshToken;
export const setTokens = client.setTokens;
export const clearAuth = client.clearAuth;
export const api = client.api;

export { ApiError } from '@apihub/shared';
