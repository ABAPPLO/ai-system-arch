import { useEffect, useState } from 'react';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Modal,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';

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

interface BillingRow {
  key: number;
  period: string;
  plan_name: string;
  total_calls: number;
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
  const [exportData, setExportData] = useState<ExportData | null>(null);
  const [consents, setConsents] = useState<ConsentItem[]>([]);
  const [activeSection, setActiveSection] = useState<Section>('account');

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

  useEffect(() => {
    void loadData();
  }, []);

  const handleExport = async () => {
    try {
      const data = await api.get<ExportData>('/v1/portal/auth/account/export');
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: 'application/json',
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `apiHub-personal-data-${new Date().toISOString().split('T')[0]}.json`;
      a.click();
      URL.revokeObjectURL(url);
      message.success('数据已导出');
    } catch (e) {
      message.error(e instanceof Error ? e.message : '导出失败');
    }
  };

  const handleWithdrawClick = () => {
    Modal.confirm({
      title: '撤回数据处理同意',
      content:
        '撤回同意后，平台将停止为对应目的处理您的数据。撤回“账户管理”同意将触发账号删除流程。',
      okText: '确认撤回',
      cancelText: '取消',
      onOk: async () => {
        try {
          await api.post('/v1/portal/auth/consent/withdraw');
          message.success('同意已撤回');
          await loadData();
        } catch (e) {
          message.error(e instanceof Error ? e.message : '撤回失败');
        }
      },
    });
  };

  const handleDeleteClick = () => {
    Modal.confirm({
      title: '永久删除账号',
      content:
        '此操作将永久删除您的账号和所有关联数据，无法恢复。确认继续？',
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await api.del('/v1/portal/auth/account');
          message.success('账号已删除');
          clearAuth();
          window.location.href = '/register';
        } catch (e) {
          message.error(e instanceof Error ? e.message : '删除失败');
        }
      },
    });
  };

  if (loading) {
    return (
      <PageContainer header={{ title: '隐私与数据' }}>
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Spin />
        </div>
      </PageContainer>
    );
  }

  const billingRows: BillingRow[] = (exportData?.billing_records || [])
    .slice(0, 6)
    .map((r, i) => ({
      key: i,
      period: r.period,
      plan_name: r.plan_name,
      total_calls: r.total_calls,
    }));

  const billingColumns: ColumnsType<BillingRow> = [
    { title: '周期', dataIndex: 'period' },
    { title: 'Plan', dataIndex: 'plan_name' },
    {
      title: '调用',
      dataIndex: 'total_calls',
      align: 'right',
      render: (v: number) => v.toLocaleString(),
    },
  ];

  const consentColumns: ColumnsType<ConsentItem> = [
    {
      title: '目的',
      dataIndex: 'purpose',
      render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
    },
    {
      title: '说明',
      dataIndex: 'description',
      render: (v: string) => (
        <Typography.Text type="secondary">{v}</Typography.Text>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      align: 'center',
      render: (v: string) => (
        <Tag color={v === 'granted' ? 'green' : 'default'}>
          {v === 'granted' ? '已同意' : '已撤回'}
        </Tag>
      ),
    },
    {
      title: '时间',
      dataIndex: 'updated_at',
      align: 'right',
      render: (v: string) => dayjs(v).format('YYYY-MM-DD'),
    },
  ];

  return (
    <PageContainer header={{ title: '隐私与数据' }}>
      <Typography.Paragraph type="secondary">
        管理您的个人数据和隐私设置
      </Typography.Paragraph>

      {err && (
        <Alert
          type="error"
          showIcon
          message={err}
          style={{ marginBottom: 16 }}
        />
      )}

      <Tabs
        activeKey={activeSection}
        onChange={(k) => setActiveSection(k as Section)}
        items={[
          {
            key: 'account',
            label: '个人信息',
            children: exportData && (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <Card title="账户信息">
                  <Descriptions column={1} size="small">
                    <Descriptions.Item label="邮箱">
                      {exportData.account.email}
                    </Descriptions.Item>
                    <Descriptions.Item label="手机">
                      {exportData.account.phone || '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label="姓名">
                      {exportData.account.name || '—'}
                    </Descriptions.Item>
                    <Descriptions.Item label="状态">
                      <Tag
                        color={
                          exportData.account.status === 'active'
                            ? 'green'
                            : 'default'
                        }
                      >
                        {exportData.account.status}
                      </Tag>
                    </Descriptions.Item>
                    <Descriptions.Item label="注册于">
                      {dayjs(exportData.account.created_at).format('YYYY-MM-DD')}
                    </Descriptions.Item>
                  </Descriptions>
                </Card>

                <Card title="租户与会员">
                  {exportData.tenants.length === 0 ? (
                    <Empty description="无租户关联" />
                  ) : (
                    <Space direction="vertical">
                      {exportData.tenants.map((t, i) => (
                        <Space key={i}>
                          <Typography.Text type="secondary">租户：</Typography.Text>
                          <Typography.Text code>{t.tenant_id}</Typography.Text>
                          <Typography.Text type="secondary">角色：</Typography.Text>
                          <Tag>{t.role}</Tag>
                        </Space>
                      ))}
                    </Space>
                  )}
                </Card>

                <Card title="应用与密钥">
                  {exportData.apps.length === 0 ? (
                    <Empty description="无应用" />
                  ) : (
                    <Space wrap>
                      {exportData.apps.map((a) => (
                        <Space key={a.id}>
                          <Typography.Text>{a.name}</Typography.Text>
                          <Tag
                            color={
                              a.status === 'active' ? 'green' : 'default'
                            }
                          >
                            {a.status}
                          </Tag>
                        </Space>
                      ))}
                    </Space>
                  )}
                </Card>

                <Card title="最近账单">
                  {billingRows.length === 0 ? (
                    <Empty description="无账单记录" />
                  ) : (
                    <Table<BillingRow>
                      rowKey="key"
                      size="small"
                      columns={billingColumns}
                      dataSource={billingRows}
                      pagination={false}
                    />
                  )}
                </Card>
              </Space>
            ),
          },
          {
            key: 'consent',
            label: '同意管理',
            children: (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <Card title="数据处理同意">
                  <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
                    您注册时已同意以下数据处理目的。您可以随时撤回。
                  </Typography.Paragraph>
                  {consents.length === 0 ? (
                    <Empty description="暂无同意记录" />
                  ) : (
                    <Table<ConsentItem>
                      rowKey={(r) => r.purpose + r.updated_at}
                      size="small"
                      columns={consentColumns}
                      dataSource={consents}
                      pagination={false}
                    />
                  )}
                </Card>

                <Alert
                  type="warning"
                  showIcon
                  message="撤回同意"
                  description={
                    <Space direction="vertical">
                      <Typography.Text>
                        撤回同意后，平台将停止为对应目的处理您的数据。撤回“账户管理”同意将触发账号删除流程。
                      </Typography.Text>
                      <Button danger onClick={handleWithdrawClick}>
                        撤回全部同意
                      </Button>
                    </Space>
                  }
                />
              </Space>
            ),
          },
        ]}
      />

      <Card title="数据操作" style={{ marginTop: 16 }}>
        <Space>
          <Button type="primary" onClick={handleExport}>
            导出个人数据（JSON）
          </Button>
          <Button danger onClick={handleDeleteClick}>
            删除账号
          </Button>
        </Space>
      </Card>
    </PageContainer>
  );
}
