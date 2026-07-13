import { useEffect, useState } from 'react';
import { api } from '../api/client';

function fmtCents(c: number): string { return `¥${(c / 100).toFixed(2)}`; }

export function Billing() {
  const [items, setItems] = useState<any[]>([]);
  const [period, setPeriod] = useState(() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`; });
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);

  const fetchData = async () => {
    setLoading(true);
    try {
      const r = await api.get<any>('/v1/admin/billing/summary', { period, search });
      setItems(r.items || []);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchData(); }, [period, search]);

  return (
    <div className="p-4">
      <h1 className="text-2xl font-bold mb-4">计费管理</h1>
      <div className="flex gap-2 mb-4 items-center">
        <input type="month" value={period} onChange={e => setPeriod(e.target.value)} className="border rounded px-3 py-1" />
        <button onClick={fetchData} className="bg-blue-600 text-white px-4 py-1 rounded">刷新</button>
        <input placeholder="搜索租户..." value={search} onChange={e => setSearch(e.target.value)} className="border rounded px-3 py-1 ml-auto" />
      </div>
      {loading ? <div className="animate-spin w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full" /> : (
        <table className="w-full text-sm">
          <thead><tr className="border-b"><th className="text-left py-2">租户</th><th>Plan</th><th className="text-right">调用量</th><th className="text-right">费用</th><th className="text-right">状态</th></tr></thead>
          <tbody>{items.map((i, idx) => (
            <tr key={i.tenant_id || idx} className="border-b hover:bg-gray-50">
              <td className="py-2">{i.tenant_id}</td>
              <td className="text-center">{i.plan_name}</td>
              <td className="text-right">{(i.total_calls||0).toLocaleString()}</td>
              <td className="text-right">{fmtCents((i.base_cents||0)+(i.overage_cents||0))}</td>
              <td className="text-right"><span className={`text-xs px-1.5 py-0.5 rounded ${i.status === 'invoiced' ? 'bg-green-100 text-green-700' : i.status === 'pending' ? 'bg-yellow-100 text-yellow-700' : 'bg-gray-100'}`}>{i.status}</span></td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}
