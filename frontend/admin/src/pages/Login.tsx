import { useState } from 'react';
import { Card, Button, Typography, Alert, Space } from 'antd';

import { api } from '../api/client';

/**
 * Admin 登录 —— 钉钉 OAuth2 SSO。
 * 点「钉钉登录」→ 调 auth /v1/auth/dingtalk/authorize 拿授权 URL → 跳钉钉扫码 →
 * 回跳 /login/callback?code=..&state=.. 由 LoginCallback 换 JWT。
 * 身份（isPlatformAdmin/tenantId）由后端 JWT 签发，前端不再伪造。
 */
export default function Login() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        </Space>
      </Card>
    </div>
  );
}
