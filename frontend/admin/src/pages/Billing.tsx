import { useCallback, useEffect, useState } from 'react';
import { PageContainer } from '@ant-design/pro-components';
import {
  Button,
  Card,
  DatePicker,
  Input,
  Space,
  Spin,
  Table,
  Tag,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import type { ColumnsType } from 'antd/es/table';

import { api } from '../api/client';

function fmtCents(c: number): string {
  return `¥${(c / 100).toFixed(2)}`;
}

interface BillingItem {
  tenant_id: string;
  plan_name: string | null;
  total_calls: number | null;
  base_cents: number | null;
  overage_cents: number | null;
  status: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  invoiced: 'green',
  pending: 'orange',
};

function currentPeriod(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

export function Billing() {
  const [items, setItems] = useState<BillingItem[]>([]);
  const [period, setPeriod] = useState<string>(currentPeriod);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get<{ items: BillingItem[] }>(
        '/api/billing/v1/admin/billing/summary',
        { period, search: search || undefined },
      );
      setItems(r.items || []);
    } catch (e) {
      // billing-svc 属 P3，端点/代理未就绪时静默置空（后续单独接线）
      console.error(e);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [period, search]);

  useEffect(() => {
    void fetchData();
  }, [fetchData]);

  const columns: ColumnsType<BillingItem> = [
    { title: '租户', dataIndex: 'tenant_id', key: 'tenant_id' },
    {
      title: 'Plan',
      dataIndex: 'plan_name',
      key: 'plan_name',
      align: 'center',
      render: (v: string | null) => v || '—',
    },
    {
      title: '调用量',
      dataIndex: 'total_calls',
      key: 'total_calls',
      align: 'right',
      render: (v: number | null) => (v ?? 0).toLocaleString(),
    },
    {
      title: '费用',
      key: 'fee',
      align: 'right',
      render: (_v, r) => fmtCents((r.base_cents ?? 0) + (r.overage_cents ?? 0)),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      align: 'right',
      render: (v: string | null) =>
        v ? <Tag color={STATUS_COLOR[v] ?? 'default'}>{v}</Tag> : '—',
    },
  ];

  return (
    <PageContainer header={{ title: '计费管理' }}>
      <Card>
        <Space style={{ marginBottom: 16 }} wrap>
          <DatePicker
            picker="month"
            value={dayjs(period + '-01')}
            onChange={(v) => v && setPeriod(v.format('YYYY-MM'))}
            allowClear={false}
          />
          <Button icon={<ReloadOutlined />} onClick={() => void fetchData()}>
            刷新
          </Button>
          <Input.Search
            placeholder="搜索租户..."
            allowClear
            enterButton
            style={{ width: 240, marginLeft: 'auto' }}
            onSearch={(v) => setSearch(v)}
          />
        </Space>
        <Table<BillingItem>
          rowKey="tenant_id"
          columns={columns}
          dataSource={items}
          loading={loading}
          pagination={{ pageSize: 20 }}
          locale={{ emptyText: loading ? <Spin /> : '暂无数据' }}
        />
      </Card>
    </PageContainer>
  );
}
