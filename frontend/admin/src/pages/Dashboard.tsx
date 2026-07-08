import { ProCard, StatisticCard } from '@ant-design/pro-components';
import { Col, Row, Table, Tag, Typography } from 'antd';
import useSWR from 'swr';
import dayjs from 'dayjs';

import { api } from '../api/client';
import type { DashboardResponse, AuditListItem } from '../api/types';

export default function Dashboard() {
  const { data, error, isLoading } = useSWR<DashboardResponse>(
    '/api/admin/v1/admin/dashboard',
    (url: string) => api.get<DashboardResponse>(url),
  );

  if (isLoading) return <div>加载中...</div>;
  if (error) return <div>加载失败：{(error as Error).message}</div>;
  if (!data) return null;

  const columns = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string) => dayjs(v).format('MM-DD HH:mm:ss'),
    },
    { title: '动作', dataIndex: 'action', width: 200 },
    { title: '资源类型', dataIndex: 'resource_type', width: 120 },
    { title: '资源 ID', dataIndex: 'resource_id' },
    { title: '操作人', dataIndex: 'actor_id', width: 120 },
    {
      title: '租户',
      dataIndex: 'tenant_id',
      width: 100,
      render: (v: string) => <Tag>{v}</Tag>,
    },
  ];

  return (
    <>
      <Typography.Title level={4}>平台概览</Typography.Title>

      <Row gutter={16} style={{ margin: '0 0 16px 0' }}>
        <Col span={8}>
          <StatisticCard
            statistic={{ title: '今日审计事件', value: data.audit_today, suffix: '条' }}
          />
        </Col>
        <Col span={8}>
          <StatisticCard
            statistic={{ title: '近 7 天审计事件', value: data.audit_7d, suffix: '条' }}
          />
        </Col>
        <Col span={8}>
          <StatisticCard
            statistic={{
              title: '租户数',
              value: Object.keys(data.tenants || {}).length,
              suffix: '个',
            }}
          />
        </Col>
      </Row>

      <ProCard title="最近审计事件" headerBordered>
        <Table<AuditListItem>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={data.top_recent_events}
          pagination={{ pageSize: 10 }}
        />
      </ProCard>
    </>
  );
}
