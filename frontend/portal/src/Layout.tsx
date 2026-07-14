import { useLocation, useNavigate } from 'react-router-dom';
import { useStore } from './store';

const NAV = [
  { path: '/apis', label: 'API 目录' },
  { path: '/apps', label: '我的应用' },
  { path: '/usage', label: '用量统计' },
  { path: '/analytics', label: '高级分析' },
  { path: '/webhooks', label: 'Webhook' },
  { path: '/plans', label: '套餐' },
  { path: '/invoices', label: '账单' },
  { path: '/privacy', label: '隐私与数据' },
];

export function Layout({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();
  const logout = useStore((s) => s.logout);
  const auth = useStore((s) => s.auth);

  return (
    <div className="min-h-screen bg-gray-50">
      <nav className="bg-white border-b sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 flex items-center justify-between h-12">
          <div className="flex items-center gap-1 overflow-x-auto">
            {NAV.map((item) => (
              <button
                key={item.path}
                onClick={() => navigate(item.path)}
                className={`px-3 py-1.5 text-sm rounded whitespace-nowrap ${
                  location.pathname === item.path || location.pathname.startsWith(item.path + '/')
                    ? 'bg-blue-100 text-blue-700 font-medium'
                    : 'text-gray-600 hover:bg-gray-100'
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-3 shrink-0 ml-4">
            <span className="text-sm text-gray-400 truncate max-w-[120px]">{auth?.user.name}</span>
            <button onClick={() => { logout(); navigate('/login'); }} className="text-sm text-gray-500 hover:text-red-600">
              退出
            </button>
          </div>
        </div>
      </nav>
      <main>{children}</main>
    </div>
  );
}
