import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface InvoiceItem {
  id: string; period: string; plan_name: string;
  total_calls: number; total_tokens: number;
  base_cents: number; overage_cents: number; total_cents: number;
  status: string; created_at: string;
}

function fmtCents(c: number): string { return `¥${(c / 100).toFixed(2)}`; }

const STATUS_BADGE: Record<string, string> = { pending: 'bg-yellow-100 text-yellow-700', invoiced: 'bg-green-100 text-green-700', adjusted: 'bg-blue-100 text-blue-700' };

export function Invoices() {
  const [items, setItems] = useState<InvoiceItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const limit = 12;

  useEffect(() => {
    setLoading(true);
    api.get<{items: InvoiceItem[]; total: number}>('/v1/portal/invoices', { limit, offset })
      .then(r => { setItems(r.items); setTotal(r.total); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [offset]);

  if (loading) return <div className="flex justify-center py-12"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;

  return (
    <div className="max-w-4xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">账单历史</h1>
      {items.length === 0 ? <div className="text-center py-12 text-gray-400"><p>暂无账单记录</p></div> : (
        <>
          <table className="w-full text-sm">
            <thead><tr className="border-b"><th className="text-left py-2">周期</th><th>Plan</th><th className="text-right">调用量</th><th className="text-right">Token</th><th className="text-right">费用</th><th className="text-right">状态</th></tr></thead>
            <tbody>{items.map(i => (
              <tr key={i.id} className="border-b hover:bg-gray-50">
                <td className="py-2">{i.period}</td>
                <td className="text-center">{i.plan_name}</td>
                <td className="text-right">{(i.total_calls||0).toLocaleString()}</td>
                <td className="text-right">{(i.total_tokens||0).toLocaleString()}</td>
                <td className="text-right">{fmtCents(i.total_cents)}</td>
                <td className="text-right"><span className={`text-xs px-1.5 py-0.5 rounded ${STATUS_BADGE[i.status]||''}`}>{i.status}</span></td>
              </tr>
            ))}</tbody>
          </table>
          {total > limit && (
            <div className="flex justify-center gap-2 mt-4">
              <button disabled={offset===0} onClick={() => setOffset(offset-limit)} className="px-3 py-1 border rounded disabled:opacity-50">上一页</button>
              <button disabled={offset+limit>=total} onClick={() => setOffset(offset+limit)} className="px-3 py-1 border rounded disabled:opacity-50">下一页</button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
