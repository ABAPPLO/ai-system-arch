import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface PlanSummary {
  code: string;
  name: string;
  price_cents: number;
  quota_included: Record<string, number>;
  features: Record<string, boolean>;
}

interface DailyUsage {
  api_id: string;
  api_name: string;
  day: string;
  calls: number;
  tokens: number;
}

interface BillingData {
  tenant_id: string;
  month: string;
  plan: PlanSummary;
  daily_usage: DailyUsage[];
  total_calls: number;
  total_tokens: number;
  remaining_calls_today: number;
}

interface PlanInfo {
  code: string;
  name: string;
  description: string | null;
  price_cents: number;
  quota_included: Record<string, number>;
  features: Record<string, boolean> | null;
  sort_order: number;
}

function fmtNum(n: number): string {
  if (n >= 10000) return (n / 10000).toFixed(1) + '万';
  return n.toLocaleString();
}

function ProgressBar({ used, total }: { used: number; total: number }) {
  const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
  const color = pct >= 100 ? 'bg-red-500' : pct >= 80 ? 'bg-orange-500' : 'bg-blue-500';
  return (
    <div className="w-full bg-gray-200 rounded h-2 mt-1">
      <div className={`${color} h-2 rounded`} style={{ width: `${pct}%` }} />
      {pct >= 100 && <p className="text-xs text-red-600 mt-1">超出配额</p>}
    </div>
  );
}

export function Usage() {
  const [data, setData] = useState<BillingData | null>(null);
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.get<BillingData>('/v1/portal/usage'),
      api.get<PlanInfo[]>('/v1/portal/plans'),
    ])
      .then(([b, p]) => { setData(b); setPlans(p); })
      .catch((e) => setErr(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="flex justify-center py-12"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;
  if (err) return <div className="text-red-600 p-4">{err}</div>;
  if (!data) return null;

  const dpd = data.plan.quota_included.calls_per_day || 0;
  const tpm = data.plan.quota_included.tokens_per_month || 0;

  const apiMap = new Map<string, { calls: number; tokens: number }>();
  data.daily_usage.forEach((u) => {
    const key = u.api_name || u.api_id;
    const prev = apiMap.get(key) || { calls: 0, tokens: 0 };
    apiMap.set(key, { calls: prev.calls + u.calls, tokens: prev.tokens + u.tokens });
  });
  const apiDetails = Array.from(apiMap.entries()).sort((a, b) => b[1].calls - a[1].calls);

  return (
    <div className="max-w-4xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">用量统计</h1>
      <p className="text-sm text-gray-500 mb-4">{data.month}</p>

      <div className="border rounded-lg p-4 mb-4 bg-blue-50">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-gray-500">当前 Plan</p>
            <p className="text-lg font-bold">{data.plan.name}</p>
          </div>
          <a href="/plans" className="text-blue-600 text-sm">升级 Plan →</a>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="border rounded-lg p-4">
          <p className="text-sm text-gray-500">调用次数</p>
          <p className="text-xl font-bold">{fmtNum(data.total_calls)} / {fmtNum(dpd)}</p>
          <ProgressBar used={data.total_calls} total={dpd} />
          <p className="text-xs text-gray-400 mt-1">今日剩余: {fmtNum(data.remaining_calls_today)}</p>
        </div>
        <div className="border rounded-lg p-4">
          <p className="text-sm text-gray-500">Token 消耗</p>
          <p className="text-xl font-bold">{fmtNum(data.total_tokens)} / {fmtNum(tpm)}</p>
          <ProgressBar used={data.total_tokens} total={tpm} />
        </div>
      </div>

      <div className="border rounded-lg p-4 mb-6">
        <h2 className="font-semibold mb-2">当前: {data.plan.name}</h2>
        <table className="w-full text-sm">
          <thead><tr className="border-b"><th className="text-left py-2">Plan</th><th className="text-right py-2">月费</th><th className="text-right py-2">日调用</th><th className="text-right py-2">SDK</th><th className="text-right py-2" /></tr></thead>
          <tbody>
            {plans.map((p) => (
              <tr key={p.code} className={p.code === data.plan.code ? 'bg-blue-50' : ''}>
                <td className="py-1.5">{p.name}</td>
                <td className="text-right">{p.price_cents > 0 ? `¥${p.price_cents / 100}/月` : '免费'}</td>
                <td className="text-right">{fmtNum(p.quota_included.calls_per_day || 0)}</td>
                <td className="text-right">{p.features?.sdk ? '✓' : '✗'}</td>
                <td className="text-right">{p.code === data.plan.code ? '当前' : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="border rounded-lg p-4">
        <h2 className="font-semibold mb-2">按 API 明细</h2>
        {apiDetails.length === 0 ? (
          <p className="text-gray-400 text-sm">本月尚无 API 调用记录</p>
        ) : (
          <table className="w-full text-sm">
            <thead><tr className="border-b"><th className="text-left py-2">API</th><th className="text-right py-2">调用</th><th className="text-right py-2">Token</th><th className="text-right py-2">占比</th></tr></thead>
            <tbody>
              {apiDetails.map(([api, u]) => (
                <tr key={api} className="border-b">
                  <td className="py-1.5">{api}</td>
                  <td className="text-right">{fmtNum(u.calls)}</td>
                  <td className="text-right">{fmtNum(u.tokens)}</td>
                  <td className="text-right">{data.total_calls > 0 ? ((u.calls / data.total_calls) * 100).toFixed(0) + '%' : '0%'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
