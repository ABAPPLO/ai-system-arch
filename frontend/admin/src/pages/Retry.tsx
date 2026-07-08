import { useState } from 'react';
import {
  PageContainer,
  ProTable,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import {
  Button,
  Drawer,
  Tag,
  Typography,
  Space,
  message,
  Popconfirm,
  Descriptions,
  Table,
} from 'antd';
import { ReloadOutlined, StopOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import useSWR from 'swr';

import { api } from '../api/client';
import type {
  RetryStatus,
  RetryTaskRow,
  RetryTaskDetail,
  RetryStats,
} from '../api/types';

const STATUS_COLOR: Record<RetryStatus, string> = {
  pending: 'orange',
  running: 'blue',
  succeeded: 'green',
  dead: 'red',
  ignored: 'default',
};

export default function Retry() {
  const [drawerId, setDrawerId] = useState<number | null>(null);
  const tableAction: ActionType | null = null;

  const { data: stats } = useSWR<RetryStats>(
    '/api/retry/v1/retry/stats',
    (url: string) => api.get<RetryStats>(url),
  );

  const columns: ProColumns<RetryTaskRow>[] = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      search: false,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      valueType: 'select',
      valueEnum: {
        pending: { text: 'pending' },
        running: { text: 'running' },
        succeeded: { text: 'succeeded' },
        dead: { text: 'dead' },
        ignored: { text: 'ignored' },
      },
      render: (_, r) => <Tag color={STATUS_COLOR[r.status]}>{r.status}</Tag>,
    },
    {
      title: 'API',
      dataIndex: 'api_id',
      width: 80,
    },
    {
      title: 'App',
      dataIndex: 'app_id',
      width: 80,
    },
    {
      title: 'trace_id',
      dataIndex: 'trace_id',
      copyable: true,
      ellipsis: true,
      search: false,
    },
    {
      title: '错误码',
      dataIndex: 'last_error_code',
      width: 120,
      search: false,
      render: (v) => v || '—',
    },
    {
      title: '错误消息',
      dataIndex: 'last_error_msg',
      ellipsis: true,
      search: false,
    },
    {
      title: '重试次数',
      dataIndex: 'retry_count',
      width: 90,
      search: false,
      render: (_, r) => `${r.retry_count} / ${r.max_attempts}`,
    },
    {
      title: '上次失败',
      dataIndex: 'last_failed_at',
      width: 160,
      search: false,
      render: (_, r) =>
        r.last_failed_at ? dayjs(r.last_failed_at).format('MM-DD HH:mm:ss') : '—',
    },
    {
      title: '操作',
      width: 160,
      search: false,
      fixed: 'right',
      render: (_, r) => (
        <Space>
          <Button size="small" onClick={() => setDrawerId(r.id)}>
            详情
          </Button>
          {(r.status === 'dead' || r.status === 'ignored' || r.status === 'pending') && (
            <>
              <Button
                size="small"
                type="link"
                icon={<ReloadOutlined />}
                onClick={() => triggerRetry(r.id)}
              >
                重试
              </Button>
              {r.status !== 'ignored' && (
                <Popconfirm
                  title="确认忽略此任务？"
                  onConfirm={() => ignoreTask(r.id)}
                >
                  <Button size="small" type="link" danger icon={<StopOutlined />}>
                    忽略
                  </Button>
                </Popconfirm>
              )}
            </>
          )}
        </Space>
      ),
    },
  ];

  async function triggerRetry(id: number) {
    try {
      await api.post(`/api/retry/v1/retry/${id}/trigger`);
      message.success(`已重新排队 #${id}`);
      tableAction?.reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  async function ignoreTask(id: number) {
    try {
      await api.post(`/api/retry/v1/retry/${id}/ignore`);
      message.success(`已忽略 #${id}`);
      tableAction?.reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <PageContainer
      header={{
        title: '失败重试',
        extra: stats ? (
          <Space>
            <Tag color="orange">pending {stats.pending}</Tag>
            <Tag color="blue">running {stats.running}</Tag>
            <Tag color="red">dead {stats.dead}</Tag>
            <Tag>ignored {stats.ignored}</Tag>
            <Tag color="green">succeeded {stats.succeeded}</Tag>
            <Tag>成功率 {(stats.success_rate * 100).toFixed(1)}%</Tag>
          </Space>
        ) : null,
      }}
    >
      <ProTable<RetryTaskRow>
        rowKey="id"
        columns={columns}
        scroll={{ x: 1200 }}
        search={{ labelWidth: 'auto' }}
        request={async (params) => {
          try {
            const data = await api.get<RetryTaskRow[]>('/api/retry/v1/retry/failed', {
              status: params.status,
              api_id: params.api_id,
              app_id: params.app_id,
              limit: params.pageSize || 20,
              offset: ((params.current || 1) - 1) * (params.pageSize || 20),
            });
            return {
              data,
              success: true,
              total: data.length < (params.pageSize || 20) ? data.length : -1,
            };
          } catch (e) {
            return { data: [], success: false, errorMessage: (e as Error).message };
          }
        }}
        pagination={{ pageSize: 20 }}
      />

      <RetryDrawer id={drawerId} onClose={() => setDrawerId(null)} />
    </PageContainer>
  );
}

function RetryDrawer({
  id,
  onClose,
}: {
  id: number | null;
  onClose: () => void;
}) {
  const { data, error, isLoading } = useSWR<RetryTaskDetail>(
    id ? `/api/retry/v1/retry/${id}` : null,
    (url: string) => api.get<RetryTaskDetail>(url),
  );

  return (
    <Drawer
      open={id !== null}
      onClose={onClose}
      width={720}
      title={data ? `重试任务 #${data.id}` : '加载中'}
    >
      {isLoading && <div>加载中...</div>}
      {error && <div>加载失败：{(error as Error).message}</div>}
      {data && (
        <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="状态">
              <Tag color={STATUS_COLOR[data.status]}>{data.status}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="trace_id">
              {data.trace_id}
            </Descriptions.Item>
            <Descriptions.Item label="API">{data.api_id}</Descriptions.Item>
            <Descriptions.Item label="App">{data.app_id}</Descriptions.Item>
            <Descriptions.Item label="重试" span={2}>
              {data.retry_count} / {data.max_attempts}（策略：{data.backoff_policy}，
              base {data.backoff_base_ms}ms）
            </Descriptions.Item>
            <Descriptions.Item label="下次重试" span={2}>
              {data.next_retry_at
                ? dayjs(data.next_retry_at).format('YYYY-MM-DD HH:mm:ss')
                : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="错误码">
              {data.last_error_code || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="失败时间">
              {data.last_failed_at
                ? dayjs(data.last_failed_at).format('MM-DD HH:mm:ss')
                : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="错误消息" span={2}>
              <Typography.Text type="danger">
                {data.last_error_msg || '—'}
              </Typography.Text>
            </Descriptions.Item>
          </Descriptions>

          <Typography.Title level={5} style={{ marginTop: 24 }}>
            重试历史（{data.attempts.length} 次）
          </Typography.Title>
          <Table
            rowKey="id"
            size="small"
            pagination={false}
            dataSource={data.attempts}
            columns={[
              {
                title: '#',
                dataIndex: 'attempt_no',
                width: 50,
              },
              {
                title: '时间',
                dataIndex: 'attempted_at',
                render: (v: string) => dayjs(v).format('MM-DD HH:mm:ss'),
              },
              {
                title: 'HTTP',
                dataIndex: 'response_status',
                width: 80,
              },
              {
                title: '错误码',
                dataIndex: 'error_code',
              },
              {
                title: '耗时',
                dataIndex: 'latency_ms',
                width: 80,
                render: (v: number | null) => (v !== null ? `${v}ms` : '—'),
              },
              {
                title: '错误消息',
                dataIndex: 'error_msg',
                ellipsis: true,
              },
            ]}
          />
        </>
      )}
    </Drawer>
  );
}
