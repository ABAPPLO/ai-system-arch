import { useRef, useState } from 'react';
import {
  PageContainer,
  ProTable,
  StatisticCard,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import {
  Button,
  Descriptions,
  Drawer,
  message,
  Tag,
  Typography,
} from 'antd';
import { EyeOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import useSWR from 'swr';

import { api, downloadCsv } from '../api/client';
import type { AuditDetail, AuditListItem, AuditStats } from '../api/types';

const ACTOR_COLOR: Record<string, string> = {
  user: 'blue',
  app: 'green',
  system: 'default',
};

export default function AuditLog() {
  const actionRef = useRef<ActionType | null>(null);
  const [drawerId, setDrawerId] = useState<number | null>(null);

  const { data: stats } = useSWR<AuditStats>(
    '/api/admin/v1/admin/audit/stats',
    (u: string) => api.get<AuditStats>(u),
  );

  async function doExport() {
    try {
      await downloadCsv(
        '/api/admin/v1/admin/audit/export/csv',
        'audit_log.csv',
      );
      message.success('已导出');
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  const columns: ProColumns<AuditListItem>[] = [
    { title: 'ID', dataIndex: 'id', width: 70, search: false },
    { title: '动作', dataIndex: 'action', width: 200 },
    { title: '资源类型', dataIndex: 'resource_type', width: 130 },
    {
      title: '资源 ID',
      dataIndex: 'resource_id',
      width: 170,
      search: false,
      render: (v) => (v as string) || '—',
    },
    {
      title: '操作人',
      dataIndex: 'actor_name',
      width: 140,
      search: false,
      render: (_, r) => r.actor_name || r.actor_id || '—',
    },
    // 仅用于搜索表单（不在表格显示）
    { title: 'actor_id', dataIndex: 'actor_id', hideInTable: true },
    { title: '租户', dataIndex: 'tenant_id', width: 120 },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 170,
      search: false,
      render: (v) => dayjs(v as string).format('YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '操作',
      width: 90,
      fixed: 'right',
      search: false,
      render: (_, r) => (
        <Button
          size="small"
          icon={<EyeOutlined />}
          onClick={() => setDrawerId(r.id)}
        >
          详情
        </Button>
      ),
    },
  ];

  return (
    <PageContainer header={{ title: '审计日志（只读）' }}>
      <StatisticCard.Group direction="row" style={{ marginBlockEnd: 16 }}>
        <StatisticCard
          statistic={{
            title: '审计事件总数',
            value: stats?.total ?? '—',
            suffix: '条',
          }}
        />
        <StatisticCard
          statistic={{
            title: 'Top 操作人',
            value: stats?.top_actors?.length ?? 0,
            suffix: '类',
          }}
        />
      </StatisticCard.Group>
      <div style={{ marginBlockEnd: 16 }}>
        <span style={{ marginInlineEnd: 8, color: '#888' }}>高频动作：</span>
        {(stats?.top_actions ?? []).slice(0, 10).map((a, i) => {
          const obj = a as Record<string, unknown>;
          return (
            <Tag key={i}>
              {String(obj.action ?? '?')} · {String(obj.n ?? obj.count ?? '')}
            </Tag>
          );
        })}
      </div>
      <ProTable<AuditListItem>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 1200 }}
        search={{ labelWidth: 'auto' }}
        toolBarRender={() => [
          <Button key="export" onClick={() => void doExport()}>
            导出 CSV
          </Button>,
        ]}
        request={async (params) => {
          try {
            const data = await api.get<AuditListItem[]>(
              '/api/admin/v1/admin/audit',
              {
                action: params.action,
                resource_type: params.resource_type,
                actor_id: params.actor_id,
                tenant_id: params.tenant_id,
                limit: params.pageSize || 50,
                offset:
                  ((params.current || 1) - 1) * (params.pageSize || 50),
              },
            );
            return {
              data,
              success: true,
              total:
                data.length < (params.pageSize || 50) ? data.length : -1,
            };
          } catch (e) {
            return {
              data: [],
              success: false,
              errorMessage: (e as Error).message,
            };
          }
        }}
        pagination={{ pageSize: 50 }}
      />
      <AuditDrawer id={drawerId} onClose={() => setDrawerId(null)} />
    </PageContainer>
  );
}

function AuditDrawer({
  id,
  onClose,
}: {
  id: number | null;
  onClose: () => void;
}) {
  const { data, error, isLoading } = useSWR<AuditDetail>(
    id ? `/api/admin/v1/admin/audit/${id}` : null,
    (url: string) => api.get<AuditDetail>(url),
  );

  return (
    <Drawer
      open={id !== null}
      onClose={onClose}
      width={780}
      title={data ? `审计 #${data.id}` : '加载中...'}
    >
      {isLoading && <div>加载中...</div>}
      {error && <div>加载失败：{(error as Error).message}</div>}
      {data && (
        <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="动作">{data.action}</Descriptions.Item>
            <Descriptions.Item label="时间">
              {dayjs(data.created_at).format('YYYY-MM-DD HH:mm:ss')}
            </Descriptions.Item>
            <Descriptions.Item label="资源" span={2}>
              {data.resource_type}
              {data.resource_id ? ` / ${data.resource_id}` : ''}
            </Descriptions.Item>
            <Descriptions.Item label="资源名">
              {data.resource_name || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="操作人">
              {data.actor_name || data.actor_id || '—'}{' '}
              <Tag color={ACTOR_COLOR[data.actor_type] ?? 'default'}>
                {data.actor_type}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="租户">
              {data.tenant_id}
            </Descriptions.Item>
            <Descriptions.Item label="IP">
              {data.actor_ip || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="鉴权">
              {data.auth_method || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="环境">
              {data.env || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="request_id" span={2}>
              <Typography.Text code>{data.request_id || '—'}</Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="trace_id" span={2}>
              <Typography.Text code>{data.trace_id || '—'}</Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="UA" span={2}>
              {data.user_agent || '—'}
            </Descriptions.Item>
          </Descriptions>

          <Typography.Title level={5} style={{ marginTop: 16 }}>
            detail
          </Typography.Title>
          <pre
            style={{
              background: '#f6f8fa',
              padding: 12,
              borderRadius: 6,
              maxHeight: 300,
              overflow: 'auto',
              fontSize: 12,
            }}
          >
            {JSON.stringify(data.detail, null, 2)}
          </pre>
        </>
      )}
    </Drawer>
  );
}
