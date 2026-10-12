import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { Button, Card, Result, Spin } from 'antd';
import { api } from '../api/client';

/**
 * 邮箱验证落地页：注册邮件里 /verify-email?token=... 的目标。
 * 验证成功 → 引导登录；token 无效/过期 → 可回注册页重来。
 */
export function VerifyEmail() {
  const [params] = useSearchParams();
  const token = params.get('token') || '';
  const nav = useNavigate();
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading');
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!token) {
      setState('error');
      setErr('缺少验证参数');
      return;
    }
    const ac = new AbortController();
    api
      .get(`/v1/portal/auth/verify-email?token=${encodeURIComponent(token)}`, undefined, {
        signal: ac.signal,
      })
      .then(() => {
        if (!ac.signal.aborted) setState('ok');
      })
      .catch((e) => {
        if (!ac.signal.aborted) {
          setState('error');
          setErr(e instanceof Error ? e.message : '验证失败');
        }
      });
    return () => ac.abort();
  }, [token]);

  return (
    <div style={{ maxWidth: 480, margin: '64px auto' }}>
      <Card>
        {state === 'loading' && (
          <div style={{ textAlign: 'center', padding: 48 }}>
            <Spin />
            <p style={{ marginTop: 16 }}>正在验证邮箱…</p>
          </div>
        )}
        {state === 'ok' && (
          <Result
            status="success"
            title="邮箱验证成功"
            subTitle="账号已激活，去登录开始使用 APIHub"
            extra={
              <Button type="primary" onClick={() => nav('/login')}>
                前往登录
              </Button>
            }
          />
        )}
        {state === 'error' && (
          <Result
            status="warning"
            title="验证失败"
            subTitle={err || '链接无效或已过期（30 分钟有效），请重新注册获取新邮件'}
            extra={
              <Link to="/register">
                <Button>重新注册</Button>
              </Link>
            }
          />
        )}
      </Card>
    </div>
  );
}
