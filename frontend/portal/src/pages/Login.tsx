import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, setAuth, AuthState } from '../api/client';
import { useStore } from '../store';

export function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const nav = useNavigate();
  const refresh = useStore((s) => s.refresh);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const r = await api.post<{ access_token: string; user: AuthState['user'] }>(
        '/v1/portal/auth/login',
        { email, password },
        { skipAuth: true },
      );
      setAuth(r.access_token, r.user);
      refresh();
      nav('/apps');
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <form onSubmit={submit}>
      <h2>登录</h2>
      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="邮箱" />
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="密码"
      />
      <button type="submit">登录</button>
      <p>{err}</p>
    </form>
  );
}
