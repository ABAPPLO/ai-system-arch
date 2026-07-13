import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';

export function Register() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [phone, setPhone] = useState('');
  const [name, setName] = useState('');
  const [msg, setMsg] = useState('');
  const nav = useNavigate();

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const r = await api.post<{ verify_token: string }>(
        '/v1/portal/auth/register',
        { email, password, phone, name },
        { skipAuth: true },
      );
      // dev stub：verify-email 自动激活，省去真实邮箱往返
      await api.get(`/v1/portal/auth/verify-email?token=${r.verify_token}`, undefined);
      setMsg('注册成功，跳转登录');
      setTimeout(() => nav('/login'), 800);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <form onSubmit={submit}>
      <h2>注册</h2>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="姓名" />
      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="邮箱" />
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="密码（≥8位）"
      />
      <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="手机号" />
      <button type="submit">注册</button>
      <p>{msg}</p>
    </form>
  );
}
