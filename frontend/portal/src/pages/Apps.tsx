import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface App {
  id: string;
  name: string;
  tenant_id: string;
  status: string;
}

export function Apps() {
  const [apps, setApps] = useState<App[]>([]);
  const [name, setName] = useState('');
  const [newKey, setNewKey] = useState('');

  const load = async () => setApps(await api.get<App[]>('/v1/portal/apps', undefined));
  useEffect(() => {
    load();
  }, []);

  const createApp = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.post('/v1/portal/apps', { name, type: 'external' });
    setName('');
    load();
  };

  const genKey = async (appId: string) => {
    const r = await api.post<{ api_key: string }>(
      `/v1/portal/apps/${appId}/api-keys`,
      { name: 'default' },
    );
    setNewKey(r.api_key);
  };

  return (
    <div>
      <h2>我的应用</h2>
      <form onSubmit={createApp}>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="应用名"
        />
        <button>新建应用</button>
      </form>
      <ul>
        {apps.map((a) => (
          <li key={a.id}>
            {a.name}（{a.tenant_id}）
            <button onClick={() => genKey(a.id)}>生成 API Key</button>
          </li>
        ))}
      </ul>
      {newKey && (
        <p>
          新 Key（仅显示一次）：<code>{newKey}</code>
        </p>
      )}
    </div>
  );
}
