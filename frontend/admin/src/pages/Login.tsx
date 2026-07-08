import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, Form, Input, Button, Typography, Alert, Space } from 'antd';

import { api, setAuth } from '../api/client';

/**
 * Phase 2 简化登录：
 *   - 输入 X-API-Key（dev 用本地的 ak_xxx）
 *   - 调 /v1/admin/health 验证（带 X-API-Key）—— 200 = key 有效
 *   - 把 key 写 localStorage
 *
 * Phase 3 替换为 OAuth2 / SSO（OIDC），通过 admin-bff 换 token。
 */
export default function Login() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form] = Form.useForm<{ apiKey: string; name: string }>();

  const onFinish = async ({ apiKey, name }: { apiKey: string; name: string }) => {
    setLoading(true);
    setError(null);
    try {
      // 试探：拿 dashboard。能拿到说明 key 有效且是超管
      const data = await api.get<{ audit_today: number }>('/api/admin/v1/admin/dashboard', undefined);
      // 暂时把 user 信息从表单填（Phase 3 由后端返回）
      setAuth(apiKey, {
        id: name,
        name,
        isPlatformAdmin: true,
        tenantId: 'platform',
      });
      void data; // 占位，证明请求成功
      navigate('/');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'unknown error';
      setError(`登录失败：${msg}`);
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
          <Typography.Text type="secondary">
            Phase 2 dev 登录：本地填 X-API-Key（生产走 SSO）
          </Typography.Text>

          {error && <Alert type="error" message={error} showIcon closable />}

          <Form form={form} layout="vertical" onFinish={onFinish}>
            <Form.Item
              name="name"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input placeholder="u_admin" />
            </Form.Item>
            <Form.Item
              name="apiKey"
              label="X-API-Key"
              rules={[{ required: true, message: '请输入 API Key' }]}
            >
              <Input.Password placeholder="ak_xxx" autoComplete="off" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={loading} block>
              登录
            </Button>
          </Form>
        </Space>
      </Card>
    </div>
  );
}
