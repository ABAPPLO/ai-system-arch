import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface PlanInfo {
  code: string; name: string; description: string | null;
  price_cents: number; quota_included: Record<string, number>;
  features: Record<string, boolean> | null; sort_order: number;
}

const FEAT_LABELS: Record<string, string> = { api_catalog: 'API 目录', try_it: '在线调试', sdk: 'SDK 下载' };

export function Plans() {
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [currentPlan, setCurrentPlan] = useState('');
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  useEffect(() => {
    setLoading(true); setErr('');
    Promise.all([
      api.get<PlanInfo[]>('/v1/portal/plans'),
      api.get<{plan_code: string}>('/v1/portal/subscription'),
    ]).then(([p, sub]) => { setPlans(p); setCurrentPlan(sub.plan_code); })
      .catch(e => setErr(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  const doUpgrade = async (code: string) => {
    if (!confirm(`确认升级到 ${plans.find(p=>p.code===code)?.name}？`)) return;
    try {
      await api.post('/v1/portal/subscribe', { plan_code: code, action: 'upgrade' });
      setCurrentPlan(code); alert('升级成功');
    } catch (e) { alert('升级失败'); }
  };

  if (loading) return <div className="flex justify-center py-12"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;
  if (err) return <div className="text-red-600 p-4">{err}</div>;

  return (
    <div className="max-w-5xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-2">选择 Plan</h1>
      <p className="text-gray-500 mb-6">按需选择，随时升级。</p>
      <div className="grid grid-cols-4 gap-4 mb-8">
        {plans.map(p => {
          const isCurrent = p.code === currentPlan;
          const q = p.quota_included;
          return (
            <div key={p.code} className={`border rounded-lg p-4 flex flex-col ${isCurrent ? 'border-blue-500 ring-2 ring-blue-200' : ''}`}>
              <h3 className="text-lg font-bold">{p.name}</h3>
              <p className="text-2xl font-bold mt-2">{p.price_cents > 0 ? `¥${p.price_cents/100}` : '免费'}</p>
              <p className="text-sm text-gray-400 mb-4">{p.price_cents > 0 ? '/月' : ''}</p>
              <ul className="text-sm space-y-1 mb-4 flex-1">
                <li>📞 {(q.calls_per_day || 0).toLocaleString()} 次/日</li>
                <li>🔤 {(q.tokens_per_month || 0).toLocaleString()} Token/月</li>
                {Object.entries(FEAT_LABELS).map(([k, v]) => (<li key={k}>{p.features?.[k] ? '✅' : '❌'} {v}</li>))}
              </ul>
              {isCurrent ? <span className="text-center text-sm bg-blue-100 text-blue-700 py-1 rounded">当前 Plan</span>
                : p.code === 'enterprise' ? <a href="mailto:sales@apihub.com" className="text-center text-sm bg-gray-100 py-1 rounded block">联系销售</a>
                : <button className="bg-blue-600 text-white py-1 rounded text-sm" onClick={() => doUpgrade(p.code)}>立即升级</button>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
