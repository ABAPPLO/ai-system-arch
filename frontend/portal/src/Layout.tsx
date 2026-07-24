import { useMemo } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ProLayout } from '@ant-design/pro-components';
import {
  ApiOutlined,
  AppstoreOutlined,
  BarChartOutlined,
  BellOutlined,
  CrownOutlined,
  FileTextOutlined,
  LineChartOutlined,
  LockOutlined,
} from '@ant-design/icons';
import { Dropdown } from 'antd';

import { useStore } from './store';

const ROUTES = {
  path: '/',
  routes: [
    { path: '/apis', name: 'API 目录', icon: <ApiOutlined /> },
    { path: '/apps', name: '我的应用', icon: <AppstoreOutlined /> },
    { path: '/usage', name: '用量统计', icon: <BarChartOutlined /> },
    { path: '/analytics', name: '高级分析', icon: <LineChartOutlined /> },
    { path: '/webhooks', name: 'Webhook', icon: <BellOutlined /> },
    { path: '/plans', name: '套餐', icon: <CrownOutlined /> },
    { path: '/invoices', name: '账单', icon: <FileTextOutlined /> },
    { path: '/privacy', name: '隐私与数据', icon: <LockOutlined /> },
  ],
};

export function Layout({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();
  const auth = useStore((s) => s.auth);
  const logout = useStore((s) => s.logout);

  const userMenu = useMemo(
    () => [
      {
        key: 'logout',
        label: '退出登录',
        onClick: () => {
          logout();
          navigate('/login');
        },
      },
    ],
    [logout, navigate],
  );

  return (
    <ProLayout
      title="APIHub"
      logo={null}
      layout="mix"
      fixedHeader
      fixSiderbar
      route={ROUTES}
      location={{ pathname: location.pathname }}
      menuItemRender={(item, dom) => (
        <a
          onClick={(e) => {
            e.preventDefault();
            if (item.path) navigate(item.path);
          }}
        >
          {dom}
        </a>
      )}
      avatarProps={{
        title: auth?.user.name || 'guest',
        size: 'small',
        render: (_, dom) => <Dropdown menu={{ items: userMenu }}>{dom}</Dropdown>,
      }}
    >
      {children}
    </ProLayout>
  );
}
