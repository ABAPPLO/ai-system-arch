import { useMemo } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { ProLayout } from '@ant-design/pro-components';
import {
  DashboardOutlined,
  ReloadOutlined,
  FileSearchOutlined,
  ApiOutlined,
  AuditOutlined,
  TeamOutlined,
  LineChartOutlined,
} from '@ant-design/icons';
import { Dropdown, Space, Tag } from 'antd';

import { clearAuth, getAuth } from '../api/client';

const ROUTES = {
  routes: [
    { path: '/', name: 'Dashboard', icon: <DashboardOutlined /> },
    { path: '/retry', name: '失败重试', icon: <ReloadOutlined /> },
    { path: '/change-requests', name: '评审工单', icon: <FileSearchOutlined /> },
    { path: '/apis', name: '接口管理', icon: <ApiOutlined /> },
    { path: '/audit', name: '审计日志', icon: <AuditOutlined /> },
    { path: '/calls', name: '调用日志', icon: <LineChartOutlined /> },
    { path: '/tenants', name: '租户管理', icon: <TeamOutlined /> },
  ],
};

export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const auth = getAuth();

  const userMenu = useMemo(
    () => [
      {
        key: 'logout',
        label: '退出登录',
        onClick: () => {
          clearAuth();
          navigate('/login');
        },
      },
    ],
    [navigate],
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
        render: (_, dom) => (
          <Dropdown menu={{ items: userMenu }}>
            {dom}
          </Dropdown>
        ),
      }}
      actionsRender={() => [
        <Space key="tenant" size={6}>
          <Tag color="blue">{auth?.user.tenantId ?? '—'}</Tag>
          {auth?.user.isPlatformAdmin && <Tag color="red">超管</Tag>}
        </Space>,
      ]}
    >
      <Outlet />
    </ProLayout>
  );
}
