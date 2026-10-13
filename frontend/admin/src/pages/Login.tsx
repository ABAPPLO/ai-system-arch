import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Button, Typography, Alert, Space, Divider } from 'antd';

import { api, setTokens } from '../api/client';

/**
 * Admin 登录 —— 钉钉 OAuth2 SSO。
 * 点「钉钉登录」→ 调 auth /v1/auth/dingtalk/authorize 拿授权 URL → 跳钉钉扫码 →
 * 回跳 /login/callback?code=..&state=.. 由 LoginCallback 换 JWT。
 * 身份（isPlatformAdmin/tenantId）由后端 JWT 签发，前端不再伪造。
 *
 * dev（import.meta.env.DEV）额外提供「Mock 登录」：配合 auth 的
 * DINGTALK_MOCK_MODE=true（mock 协议 code = mock:<unionId>:<name>），
 * 免真实钉钉应用即可全链路进入 admin。
 */
export default function Login() {
  const [loading, setLoading] = useState(false);
  const [mockLoading, setMockLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const start = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<{ authorize_url: string }>(
        '/api/auth/v1/auth/dingtalk/authorize',
        { redirect: `${window.location.origin}/login/callback` },
      );
      window.location.href = data.authorize_url;
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'unknown error';
      setError(`发起钉钉登录失败：${msg}`);
    } finally {
      setLoading(false);
    }
  };


  const mockLogin = async () => {
    setMockLoading(true);
    setError(null);
    try {
      const a = await api.get<{ state: string }>(
        '/api/auth/v1/auth/dingtalk/authorize',
        { redirect: `${window.location.origin}/login/callback` },
      );
      const data = await api.post<{
        access_token: string;
        refresh_token: string;
        user: { id: string; name: string; is_platform_admin: boolean; tenant_id: string };
      }>(
        '/api/auth/v1/auth/dingtalk/callback',
        { code: 'mock:UID_ADMIN:Admin', state: a.state },
        { skipAuth: true },
      );
      setTokens(data.access_token, data.refresh_token, {
        id: data.user.id,
        name: data.user.name,
        isPlatformAdmin: data.user.is_platform_admin,
        tenantId: data.user.tenant_id,
      });
      navigate('/');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'unknown error';
      setError(`Mock 登录失败（需 auth 开 DINGTALK_MOCK_MODE=true）：${msg}`);
    } finally {
      setMockLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f0f2f5',
      }}
    >
      <Card style={{ width: 400 }}>
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Typography.Title level={3} style={{ margin: 0 }}>
            APIHub Admin
          </Typography.Title>
          <Typography.Text type="secondary">使用钉钉账号登录管理控制台</Typography.Text>
          {error && <Alert type="error" message={error} showIcon closable />}
          <Button type="primary" loading={loading} onClick={start} block>
            钉钉登录
          </Button>
          {import.meta.env.DEV && (
            <>
              <Divider style={{ margin: 0 }}>dev 专用</Divider>
              <Button loading={mockLoading} onClick={mockLogin} block>
                Mock 登录（UID_ADMIN 超管）
              </Button>
            </>
          )}
        </Space>
      </Card>
    </div>
  );
}
