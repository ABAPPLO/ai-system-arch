import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Card, Form, Input, message } from 'antd';
import { api } from '../api/client';

interface RegisterForm {
  name: string;
  email: string;
  password: string;
  phone: string;
}

export function Register() {
  const [loading, setLoading] = useState(false);
  const nav = useNavigate();

  const onFinish = async (v: RegisterForm) => {
    setLoading(true);
    try {
      const r = await api.post<{ verify_token: string }>(
        '/v1/portal/auth/register',
        v,
        { skipAuth: true },
      );
      // dev stub：verify-email 自动激活，省去真实邮箱往返
      await api.get(`/v1/portal/auth/verify-email?token=${r.verify_token}`, undefined);
      message.success('注册成功，跳转登录');
      setTimeout(() => nav('/login'), 800);
    } catch (err) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 420, margin: '64px auto' }}>
      <Card title="注册 APIHub 开发者账号">
        <Form layout="vertical" onFinish={onFinish}>
          <Form.Item label="姓名" name="name" rules={[{ required: true, message: '请输入姓名' }]}>
            <Input placeholder="姓名" />
          </Form.Item>
          <Form.Item
            label="邮箱"
            name="email"
            rules={[
              { required: true, message: '请输入邮箱' },
              { type: 'email', message: '邮箱格式不正确' },
            ]}
          >
            <Input placeholder="邮箱" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, min: 8, message: '至少 8 位' }]}
          >
            <Input.Password placeholder="密码（≥8位）" />
          </Form.Item>
          <Form.Item label="手机号" name="phone" rules={[{ required: true, message: '请输入手机号' }]}>
            <Input placeholder="手机号" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>
            注册
          </Button>
        </Form>
      </Card>
    </div>
  );
}
