import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Card, Form, Input, message } from 'antd';
import { api, setTokens, type AuthState } from '../api/client';
import { useStore } from '../store';

interface LoginForm {
  email: string;
  password: string;
}

export function Login() {
  const [loading, setLoading] = useState(false);
  const nav = useNavigate();
  const refresh = useStore((s) => s.refresh);

  const onFinish = async (v: LoginForm) => {
    setLoading(true);
    try {
      const r = await api.post<{
        access_token: string;
        refresh_token: string;
        user: AuthState['user'];
      }>('/v1/portal/auth/login', v, { skipAuth: true });
      setTokens(r.access_token, r.refresh_token, r.user);
      refresh();
      nav('/apps');
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 420, margin: '64px auto' }}>
      <Card title="登录 APIHub">
        <Form layout="vertical" onFinish={onFinish}>
          <Form.Item label="邮箱" name="email" rules={[{ required: true, message: '请输入邮箱' }]}>
            <Input placeholder="邮箱" />
          </Form.Item>
          <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password placeholder="密码" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>
            登录
          </Button>
        </Form>
      </Card>
    </div>
  );
}
