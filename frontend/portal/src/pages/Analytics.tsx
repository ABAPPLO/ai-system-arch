import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface FunnelStep { api_id: string; path: string; }
interface FunnelItem { trace_id: string; step_count: number; steps: FunnelStep[]; }
interface CoOccurrenceItem { api_a: string; path_a: string; api_b: string; path_b: string; pair_count: number; }

export function Analytics() {
  const [funnel, setFunnel] = useState<FunnelItem[]>([]);
  const [cooccur, setCooccur] = useState<CoOccurrenceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.get<FunnelItem[]>('/v1/portal/analytics/funnel'),
      api.get<CoOccurrenceItem[]>('/v1/portal/analytics/co-occurrence'),
    ])
      .then(([f, c]) => { setFunnel(f); setCooccur(c); })
      .catch((e) => setErr(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="flex justify-center py-12"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;
  if (err) return <div className="text-red-600 p-4">{err}</div>;

  const stepDist = funnel.reduce<Record<number, number>>((acc, f) => {
    acc[f.step_count] = (acc[f.step_count] || 0) + 1;
    return acc;
  }, {});
  const maxDist = Math.max(...Object.values(stepDist), 1);

  return (
    <div className="max-w-5xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-2">高级分析</h1>
      <p className="text-sm text-gray-500 mb-6">API 调用行为路径与模式分析</p>

      <div className="border rounded-lg p-4 mb-6">
        <h2 className="font-semibold mb-3">调用链步数分布</h2>
        <p className="text-xs text-gray-500 mb-3">单个 trace 中的 API 调用次数——反映调用链长度</p>
        {Object.keys(stepDist).length === 0 ? (
          <p className="text-sm text-gray-400">暂无数据</p>
        ) : (
          <div className="space-y-2">
            {Object.entries(stepDist).sort(([a], [b]) => Number(a) - Number(b)).map(([steps, count]) => (
              <div key={steps} className="flex items-center gap-3">
                <span className="text-sm w-16 text-right">{steps} 步</span>
                <div className="flex-1 bg-gray-100 rounded h-6">
                  <div className="bg-blue-500 h-6 rounded transition-all" style={{ width: `${(count / maxDist) * 100}%` }} />
                </div>
                <span className="text-sm text-gray-500 w-16">{count} trace</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="border rounded-lg p-4 mb-6">
        <h2 className="font-semibold mb-3">最近调用序列</h2>
        {funnel.length === 0 ? (
          <p className="text-sm text-gray-400">暂无数据</p>
        ) : (
          <div className="space-y-3">
            {funnel.slice(0, 10).map((f) => (
              <div key={f.trace_id} className="text-sm border-b pb-2">
                <p className="text-gray-400 text-xs mb-1 font-mono">{f.trace_id.slice(0, 16)}...</p>
                <div className="flex flex-wrap gap-1">
                  {f.steps.map((s, i) => (
                    <span key={i} className="inline-flex items-center gap-1">
                      <span className="bg-blue-50 text-blue-700 px-1.5 py-0.5 rounded text-xs">{s.path || s.api_id}</span>
                      {i < f.steps.length - 1 && <span className="text-gray-300">→</span>}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="border rounded-lg p-4 mb-6">
        <h2 className="font-semibold mb-3">API 共现</h2>
        <p className="text-xs text-gray-500 mb-3">同一 trace 中的 API 对——反映组合使用模式</p>
        {cooccur.length === 0 ? (
          <p className="text-sm text-gray-400">暂无数据（需至少 3 对共现）</p>
        ) : (
          <table className="w-full text-sm">
            <thead><tr className="border-b"><th className="text-left py-2">API A</th><th className="text-left py-2">API B</th><th className="text-right py-2">共现次数</th></tr></thead>
            <tbody>
              {cooccur.slice(0, 20).map((c, i) => (
                <tr key={i} className="border-b hover:bg-gray-50">
                  <td className="py-1.5"><span className="bg-blue-50 text-blue-700 px-1.5 py-0.5 rounded text-xs">{c.path_a || c.api_a}</span></td>
                  <td className="py-1.5"><span className="bg-green-50 text-green-700 px-1.5 py-0.5 rounded text-xs">{c.path_b || c.api_b}</span></td>
                  <td className="text-right py-1.5 font-medium">{c.pair_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
