import {
  PageContainer,
  ProTable,
  type ProColumns,
} from '@ant-design/pro-components';
import { Tag } from 'antd';
import dayjs from 'dayjs';

import { api } from '../api/client';

interface InvoiceItem {
  id: string;
  period: string;
  plan_name: string;
  total_calls: number;
  total_tokens: number;
  base_cents: number;
  overage_cents: number;
  total_cents: number;
  status: string;
  created_at: string;
}

function fmtCents(c: number): string {
  return `¥${(c / 100).toFixed(2)}`;
}

const STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  invoiced: 'green',
  adjusted: 'blue',
};

const PAGE_SIZE = 12;

export function Invoices() {
  const columns: ProColumns<InvoiceItem>[] = [
    { title: '周期', dataIndex: 'period', width: 130 },
    { title: 'Plan', dataIndex: 'plan_name', width: 130 },
    {
      title: '调用量',
      dataIndex: 'total_calls',
      width: 110,
      align: 'right',
      render: (_, r) => (r.total_calls || 0).toLocaleString(),
    },
    {
      title: 'Token',
      dataIndex: 'total_tokens',
      width: 120,
      align: 'right',
      render: (_, r) => (r.total_tokens || 0).toLocaleString(),
    },
    {
      title: '费用',
      dataIndex: 'total_cents',
      width: 120,
      align: 'right',
      render: (_, r) => fmtCents(r.total_cents),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (_, r) => (
        <Tag color={STATUS_COLOR[r.status] ?? 'default'}>{r.status}</Tag>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 160,
      render: (v) => dayjs(v as string).format('YYYY-MM-DD HH:mm'),
    },
  ];

  return (
    <PageContainer header={{ title: '账单历史' }}>
      <ProTable<InvoiceItem>
        rowKey="id"
        columns={columns}
        search={false}
        pagination={{ pageSize: PAGE_SIZE }}
        scroll={{ x: 900 }}
        request={async (params) => {
          const limit = params.pageSize || PAGE_SIZE;
          const offset = ((params.current || 1) - 1) * limit;
          try {
            const r = await api.get<{ items: InvoiceItem[]; total: number }>(
              '/v1/portal/invoices',
              { limit, offset },
            );
            return {
              data: r.items,
              success: true,
              total: r.total,
            };
          } catch (e) {
            return {
              data: [],
              success: false,
              errorMessage: (e as Error).message,
            };
          }
        }}
      />
    </PageContainer>
  );
}
