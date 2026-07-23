import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Spin, Alert } from 'antd';

import { api, setTokens } from '../api/client';

interface SsoResponse {
  access_token: string;
  refresh_token: string;
  user: { id: string; name: string; is_platform_admin: boolean; tenant_id: string };
}

/** 钉钉回跳处理：code+state → 换 JWT → 进首页。 */
export default function LoginCallback() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  // 防 dev React StrictMode 双触发 effect（钉钉 code 一次性，第二次 POST 会 401）。prod 无双触发。
  const called = useRef(false);

  useEffect(() => {
    const code = params.get('code');
    const state = params.get('state');
    if (!code || !state) {
      setError('缺少 code/state 参数');
      return;
    }
    if (called.current) return;
    called.current = true;
    api
      .post<SsoResponse>(
        '/api/auth/v1/auth/dingtalk/callback',
        { code, state },
        { skipAuth: true },
      )
      .then((data) => {
        setTokens(data.access_token, data.refresh_token, {
          id: data.user.id,
          name: data.user.name,
          isPlatformAdmin: data.user.is_platform_admin,
          tenantId: data.user.tenant_id,
        });
        navigate('/');
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : '登录失败');
      });
  }, [params, navigate]);

  if (error) {
    return (
      <div style={{ padding: 64, textAlign: 'center' }}>
        <Alert type="error" message={`登录回调失败：${error}`} showIcon />
      </div>
    );
  }
  return (
    <div style={{ padding: 64, textAlign: 'center' }}>
      <Spin size="large" />
    </div>
  );
}
