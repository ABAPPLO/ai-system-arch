import { useEffect, useState } from 'react';
import { api, clearAuth } from '../api/client';

interface AccountInfo {
  email: string;
  phone: string;
  name: string;
  verification_level: string;
  status: string;
  created_at: string;
}

interface ConsentItem {
  purpose: string;
  description: string;
  status: string;
  granted_at: string;
  updated_at: string;
}

interface ExportData {
  user_id: string;
  exported_at: string;
  account: AccountInfo;
  tenants: { tenant_id: string; role: string }[];
  apps: { id: string; name: string; status: string }[];
  api_keys: { id: string; name: string; status: string }[];
  billing_records: { period: string; plan_name: string; total_calls: number }[];
}

type Section = 'account' | 'consent';

export function Privacy() {
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [success, setSuccess] = useState('');
  const [exportData, setExportData] = useState<ExportData | null>(null);
  const [consents, setConsents] = useState<ConsentItem[]>([]);
  const [activeSection, setActiveSection] = useState<Section>('account');
  const [confirmDelete, setConfirmDelete] = useState(false);

  const loadData = async () => {
    setLoading(true);
    setErr('');
    try {
      const [exp, con] = await Promise.all([
        api.get<ExportData>('/v1/portal/auth/account/export'),
        api.get<{ consents: ConsentItem[] }>('/v1/portal/auth/consent'),
      ]);
      setExportData(exp);
      setConsents(con.consents);
    } catch (e) {
      setErr(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadData(); }, []);

  const handleExport = async () => {
    try {
      const data = await api.get<ExportData>('/v1/portal/auth/account/export');
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `apiHub-personal-data-${new Date().toISOString().split('T')[0]}.json`;
      a.click();
      URL.revokeObjectURL(url);
      setSuccess('数据已导出');
    } catch (e) {
      setErr(e instanceof Error ? e.message : '导出失败');
    }
  };

  const handleWithdrawConsent = async () => {
    if (!window.confirm('撤回同意后，您的数据将停止用于对应目的。确定继续？')) return;
    try {
      await api.post('/v1/portal/auth/consent/withdraw');
      setSuccess('同意已撤回');
      loadData();
    } catch (e) {
      setErr(e instanceof Error ? e.message : '撤回失败');
    }
  };

  const handleDeleteAccount = async () => {
    if (!window.confirm('这将永久删除您的账号和所有个人数据。此操作不可撤销。确定继续？')) return;
    if (!window.confirm('再次确认：账号删除后无法恢复。')) return;
    try {
      await api.del('/v1/portal/auth/account');
      clearAuth();
      window.location.href = '/register';
    } catch (e) {
      setErr(e instanceof Error ? e.message : '删除失败');
    }
  };

  if (loading) {
    return <div className="flex justify-center py-12"><div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" /></div>;
  }

  const tab = (section: Section, label: string) => (
    <button
      onClick={() => setActiveSection(section)}
      className={`px-4 py-2 text-sm font-medium border-b-2 ${activeSection === section ? 'border-blue-500 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700'}`}
    >
      {label}
    </button>
  );

  return (
    <div className="max-w-3xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-2">隐私与数据</h1>
      <p className="text-sm text-gray-500 mb-4">管理您的个人数据和隐私设置</p>

      {err && <div className="bg-red-100 border border-red-300 text-red-700 px-4 py-2 rounded mb-4">{err}</div>}
      {success && <div className="bg-green-100 border border-green-300 text-green-700 px-4 py-2 rounded mb-4">{success}</div>}

      <div className="flex gap-2 mb-6 border-b">
        {tab('account', '个人信息')}
        {tab('consent', '同意管理')}
      </div>

      {activeSection === 'account' && exportData && (
        <div>
          <div className="border rounded-lg p-4 mb-4">
            <h2 className="font-semibold mb-3">账户信息</h2>
            <dl className="text-sm space-y-2">
              <div className="flex"><dt className="w-24 text-gray-500">邮箱</dt><dd>{exportData.account.email}</dd></div>
              <div className="flex"><dt className="w-24 text-gray-500">手机</dt><dd>{exportData.account.phone || '-'}</dd></div>
              <div className="flex"><dt className="w-24 text-gray-500">姓名</dt><dd>{exportData.account.name || '-'}</dd></div>
              <div className="flex"><dt className="w-24 text-gray-500">状态</dt><dd>{exportData.account.status}</dd></div>
              <div className="flex"><dt className="w-24 text-gray-500">注册于</dt><dd>{new Date(exportData.account.created_at).toLocaleDateString()}</dd></div>
            </dl>
          </div>

          <div className="border rounded-lg p-4 mb-4">
            <h2 className="font-semibold mb-3">租户与会员</h2>
            {exportData.tenants.length === 0 ? (
              <p className="text-sm text-gray-400">无租户关联</p>
            ) : (
              <ul className="text-sm space-y-1">
                {exportData.tenants.map((t, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className="text-gray-500">租户:</span> {t.tenant_id}
                    <span className="text-gray-500 ml-3">角色:</span> {t.role}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="border rounded-lg p-4 mb-4">
            <h2 className="font-semibold mb-3">应用与密钥</h2>
            {exportData.apps.length === 0 ? (
              <p className="text-sm text-gray-400">无应用</p>
            ) : (
              <ul className="text-sm space-y-1">
                {exportData.apps.map((a) => (
                  <li key={a.id} className="flex items-center gap-3">
                    <span>{a.name}</span>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${a.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>{a.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="border rounded-lg p-4 mb-6">
            <h2 className="font-semibold mb-3">最近账单</h2>
            {exportData.billing_records.length === 0 ? (
              <p className="text-sm text-gray-400">无账单记录</p>
            ) : (
              <table className="w-full text-sm">
                <thead><tr className="border-b"><th className="text-left py-1">周期</th><th className="text-left py-1">Plan</th><th className="text-right py-1">调用</th></tr></thead>
                <tbody>
                  {exportData.billing_records.slice(0, 6).map((r, i) => (
                    <tr key={i} className="border-b">
                      <td className="py-1">{r.period}</td>
                      <td className="py-1">{r.plan_name}</td>
                      <td className="text-right py-1">{r.total_calls.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {activeSection === 'consent' && (
        <div>
          <div className="border rounded-lg p-4 mb-4">
            <h2 className="font-semibold mb-3">数据处理同意</h2>
            <p className="text-xs text-gray-500 mb-3">您注册时已同意以下数据处理目的。您可以随时撤回。</p>
            {consents.length === 0 ? (
              <p className="text-sm text-gray-400">暂无同意记录</p>
            ) : (
              <table className="w-full text-sm">
                <thead><tr className="border-b"><th className="text-left py-2">目的</th><th className="text-left py-2">说明</th><th className="text-center py-2">状态</th><th className="text-right py-2">时间</th></tr></thead>
                <tbody>
                  {consents.map((c, i) => (
                    <tr key={i} className="border-b">
                      <td className="py-2 font-medium">{c.purpose}</td>
                      <td className="py-2 text-gray-500">{c.description}</td>
                      <td className="text-center py-2">
                        <span className={`text-xs px-1.5 py-0.5 rounded ${c.status === 'granted' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
                          {c.status === 'granted' ? '已同意' : '已撤回'}
                        </span>
                      </td>
                      <td className="text-right py-2 text-gray-400">{new Date(c.updated_at).toLocaleDateString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="border rounded-lg p-4 mb-4 bg-yellow-50">
            <h2 className="font-semibold mb-2 text-yellow-800">撤回同意</h2>
            <p className="text-sm text-yellow-700 mb-3">
              撤回同意后，平台将停止为对应目的处理您的数据。撤回"账户管理"同意将触发账号删除流程。
            </p>
            <button onClick={handleWithdrawConsent} className="bg-yellow-600 text-white px-4 py-2 rounded text-sm hover:bg-yellow-700">
              撤回全部同意
            </button>
          </div>
        </div>
      )}

      <div className="border rounded-lg p-4 mb-4">
        <h2 className="font-semibold mb-3">数据操作</h2>
        <div className="flex flex-wrap gap-3">
          <button onClick={handleExport} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">
            导出个人数据（JSON）
          </button>
          <button onClick={() => setConfirmDelete(!confirmDelete)} className="bg-red-600 text-white px-4 py-2 rounded text-sm hover:bg-red-700">
            删除账号
          </button>
        </div>
        {confirmDelete && (
          <div className="mt-4 p-4 bg-red-50 border border-red-200 rounded">
            <p className="text-sm text-red-700 mb-3"><strong>警告：</strong>此操作将永久删除您的账号和所有关联数据，无法恢复。</p>
            <button onClick={handleDeleteAccount} className="bg-red-700 text-white px-4 py-2 rounded text-sm hover:bg-red-800">
              确认删除账号
            </button>
            <button onClick={() => setConfirmDelete(false)} className="ml-2 text-sm text-gray-500 hover:text-gray-700">
              取消
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
