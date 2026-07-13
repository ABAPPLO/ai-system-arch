import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface Webhook {
  id: string;
  url: string;
  events: string[];
  status: string;
  created_at: string;
}

export function Webhooks() {
  const [hooks, setHooks] = useState<Webhook[]>([]);
  const [loading, setLoading] = useState(true);
  const [url, setUrl] = useState('');
  const [events, setEvents] = useState('');
  const [err, setErr] = useState('');
  const [testResult, setTestResult] = useState('');

  const load = async () => {
    setLoading(true);
    try { setHooks(await api.get<Webhook[]>('/v1/portal/webhooks')); }
    catch (e) { setErr(String(e)); }
    finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    await api.post('/v1/portal/webhooks', {
      url, events: events.split(',').map(s => s.trim()).filter(Boolean),
    });
    setUrl(''); setEvents('');
    load();
  };

  const remove = async (id: string) => {
    await api.del(`/v1/portal/webhooks/${id}`);
    load();
  };

  const test = async (id: string) => {
    try {
      const r = await api.post<{ success: boolean; status_code: number | null; latency_ms: number | null; error: string | null }>(`/v1/portal/webhooks/${id}/test`);
      setTestResult(r.success ? `✅ ${r.status_code} in ${r.latency_ms}ms` : `❌ ${r.error || r.status_code}`);
    } catch (e) { setTestResult(`❌ ${String(e)}`); }
    setTimeout(() => setTestResult(''), 5000);
  };

  if (loading) return <div className="flex justify-center py-8"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;

  return (
    <div className="max-w-4xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">Webhook 通知</h1>

      {err && <div className="bg-red-50 p-3 rounded text-red-700 mb-4">{err}</div>}
      {testResult && <div className="bg-blue-50 p-3 rounded text-blue-700 mb-4">{testResult}</div>}

      <form onSubmit={create} className="border rounded-lg p-4 mb-6 space-y-3">
        <h2 className="font-semibold">新增 Webhook</h2>
        <input className="w-full border rounded px-3 py-2" placeholder="回调 URL" value={url} onChange={e => setUrl(e.target.value)} />
        <input className="w-full border rounded px-3 py-2" placeholder="事件类型（逗号分隔，如 api.call.succeeded,api.call.failed）" value={events} onChange={e => setEvents(e.target.value)} />
        <button className="bg-blue-600 text-white px-4 py-2 rounded" type="submit">创建</button>
      </form>

      {hooks.length === 0 ? (
        <p className="text-gray-400 text-center py-8">暂无 Webhook</p>
      ) : (
        <div className="space-y-3">
          {hooks.map(h => (
            <div key={h.id} className="border rounded-lg p-4">
              <div className="flex items-center justify-between mb-1">
                <code className="text-sm flex-1 truncate">{h.url}</code>
                <span className={`text-xs px-2 py-0.5 rounded ${h.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>{h.status}</span>
              </div>
              <p className="text-xs text-gray-400 mb-2">事件: {h.events.join(', ')}</p>
              <div className="flex gap-2">
                <button className="text-xs px-3 py-1 border rounded hover:bg-gray-50" onClick={() => test(h.id)}>测试</button>
                <button className="text-xs px-3 py-1 border rounded text-red-600 hover:bg-red-50" onClick={() => remove(h.id)}>删除</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
