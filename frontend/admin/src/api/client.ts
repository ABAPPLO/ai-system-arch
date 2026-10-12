/**
 * Admin API client —— 共享工厂（@apihub/shared）的 admin 配置。
 *
 * 钉钉 SSO 登录后存 access/refresh JWT；401 自动 refresh 一次的实现在共享包
 * 单源维护（admin/portal 一份逻辑）。机器访问（脚本/API Key）仍可直接带
 * X-API-Key 头（本 client 仅服务浏览器 SSO 态）。
 */

import { createApiClient } from '@apihub/shared';

export interface AdminUser {
  id: string;
  name: string;
  isPlatformAdmin: boolean;
  tenantId: string;
}

export interface AuthState {
  token: string;
  user: AdminUser;
}

const client = createApiClient<AdminUser>({
  tokenStorageKey: 'apihub_admin_token',
  refreshStorageKey: 'apihub_admin_refresh',
  userStorageKey: 'apihub_admin_user',
  refreshUrl: '/api/auth/v1/auth/refresh',
  loginRedirect: '/login',
  defaultUser: { id: '', name: '', isPlatformAdmin: false, tenantId: 'platform' },
});

export const getAuth = client.getAuth;
export const getRefreshToken = client.getRefreshToken;
export const setTokens = client.setTokens;
export const clearAuth = client.clearAuth;
export const api = client.api;
export const downloadCsv = client.downloadCsv;

export { ApiError } from '@apihub/shared';
