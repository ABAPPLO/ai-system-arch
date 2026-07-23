import { useRef, useState } from 'react';
import {
  PageContainer,
  ProTable,
  StatisticCard,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import { Button, Descriptions, Drawer, message, Tag, Typography } from 'antd';
import { EyeOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import useSWR from 'swr';

import { api, downloadCsv } from '../api/client';
import type { CallDetail, CallListItem, CallStats } from '../api/types';

const TRACE = '/api/trace/v1/trace';

function statusColor(code: number): string {
  if (code >= 500) return 'red';
  if (code >= 400) return 'orange';
  if (code >= 200) return 'green';
  return 'default';
}

export default function Calls() {
  const actionRef = useRef<ActionType | null>(null);
  const [drawerTrace, setDrawerTrace] = useState<string | null>(null);

  const { data: stats } = useSWR<CallStats>(`${TRACE}/calls/stats`, (u: string) =>
    api.get<CallStats>(u),
  );

  async function doExport() {
    try {
      await downloadCsv(`${TRACE}/calls/export`, 'api_calls.csv');
      message.success('已导出');
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  const columns: ProColumns<CallListItem>[] = [
    {
      title: 'trace_id',
      dataIndex: 'trace_id',
      width: 200,
      ellipsis: true,
      copyable: true,
    },
    { title: 'api_id', dataIndex: 'api_id', width: 160, ellipsis: true },
    {
      title: '路径',
      dataIndex: 'api_path',
      ellipsis: true,
      search: false,
      render: (_, r) => (
        <Typography.Text code>
          {r.api_method} {r.api_path}
        </Typography.Text>
      ),
    },
    {
      title: 'HTTP',
      dataIndex: 'http_status',
      width: 80,
      search: false,
      render: (v) => <Tag color={statusColor(Number(v))}>{String(v)}</Tag>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      valueType: 'select',
      valueEnum: {
        all: { text: 'all' },
        success: { text: 'success' },
        failed: { text: 'failed' },
        timeout: { text: 'timeout' },
      },
    },
    {
      title: 'app_id',
      dataIndex: 'app_id',
      width: 140,
      ellipsis: true,
    },
    {
      title: '延迟(ms)',
      dataIndex: 'latency_ms',
      width: 90,
      search: false,
      render: (v) => Number(v).toLocaleString(),
    },
    {
      title: '时间',
      dataIndex: 'ts',
      width: 160,
      search: false,
      render: (v) => dayjs(v as string).format('MM-DD HH:mm:ss'),
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
          onClick={() => setDrawerTrace(r.trace_id)}
        >
          详情
        </Button>
      ),
    },
  ];

  return (
    <PageContainer header={{ title: '调用日志（只读）' }}>
      <StatisticCard.Group direction="row" style={{ marginBlockEnd: 16 }}>
        <StatisticCard
          statistic={{ title: '总调用', value: stats?.total ?? '—', suffix: '条' }}
        />
        <StatisticCard
          statistic={{
            title: '成功率',
            value:
              stats?.success_rate != null
                ? `${(stats.success_rate * 100).toFixed(1)}%`
                : '—',
          }}
        />
        <StatisticCard
          statistic={{
            title: 'P95 延迟',
            value: stats?.p95_latency_ms ?? '—',
            suffix: 'ms',
          }}
        />
        <StatisticCard
          statistic={{ title: 'QPS', value: stats?.qps ?? '—' }}
        />
        <StatisticCard
          statistic={{
            title: '失败/超时',
            value:
              stats != null
                ? `${stats.failed_count}/${stats.timeout_count}`
                : '—',
          }}
        />
      </StatisticCard.Group>

      <ProTable<CallListItem>
        rowKey="trace_id"
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
            const data = await api.get<CallListItem[]>(`${TRACE}/calls`, {
              api_id: params.api_id,
              app_id: params.app_id,
              trace_id: params.trace_id,
              status: params.status || 'all',
              limit: params.pageSize || 50,
              offset:
                ((params.current || 1) - 1) * (params.pageSize || 50),
            });
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

      <CallDrawer traceId={drawerTrace} onClose={() => setDrawerTrace(null)} />
    </PageContainer>
  );
}

function CallDrawer({
  traceId,
  onClose,
}: {
  traceId: string | null;
  onClose: () => void;
}) {
  const { data, error, isLoading } = useSWR<CallDetail>(
    traceId ? `${TRACE}/calls/${traceId}` : null,
    (u: string) => api.get<CallDetail>(u),
  );

  return (
    <Drawer
      open={traceId !== null}
      onClose={onClose}
      width={820}
      title={data ? `调用 ${data.trace_id}` : '加载中...'}
    >
      {isLoading && <div>加载中...</div>}
      {error && <div>加载失败：{(error as Error).message}</div>}
      {data && (
        <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="HTTP">
              <Tag color={statusColor(data.http_status)}>
                {data.http_status}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="成功">
              {data.is_success ? (
                <Tag color="green">success</Tag>
              ) : (
                <Tag color="red">failed</Tag>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="路径" span={2}>
              <Typography.Text code>
                {data.api_method} {data.api_path}
              </Typography.Text>
            </Descriptions.Item>
            <Descriptions.Item label="api_id">{data.api_id}</Descriptions.Item>
            <Descriptions.Item label="版本">{data.api_version}</Descriptions.Item>
            <Descriptions.Item label="app_id">{data.app_id}</Descriptions.Item>
            <Descriptions.Item label="caller_ip">
              {data.caller_ip || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="总延迟">
              {data.latency_ms} ms
            </Descriptions.Item>
            <Descriptions.Item label="backend 延迟">
              {data.backend_latency_ms ?? '—'} ms
            </Descriptions.Item>
            <Descriptions.Item label="环境">{data.env || '—'}</Descriptions.Item>
            <Descriptions.Item label="流式">
              {data.is_streaming ? '是' : '否'}
            </Descriptions.Item>
            {data.token_total != null && (
              <Descriptions.Item label="token 总量">
                {data.token_total}
                {data.ai_model ? `（${data.ai_model}）` : ''}
              </Descriptions.Item>
            )}
            <Descriptions.Item label="时间">
              {dayjs(data.ts).format('YYYY-MM-DD HH:mm:ss')}
            </Descriptions.Item>
            <Descriptions.Item label="req_id" span={2}>
              <Typography.Text code>{data.req_id || '—'}</Typography.Text>
            </Descriptions.Item>
            {data.error_type && (
              <Descriptions.Item label="错误类型" span={2}>
                <Tag color="red">{data.error_type}</Tag>
              </Descriptions.Item>
            )}
            {data.error_msg && (
              <Descriptions.Item label="错误信息" span={2}>
                <Typography.Text type="danger">{data.error_msg}</Typography.Text>
              </Descriptions.Item>
            )}
          </Descriptions>
        </>
      )}
    </Drawer>
  );
}
