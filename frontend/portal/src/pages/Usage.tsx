import { useEffect, useState } from 'react';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { api } from '../api/client';

interface PlanSummary {
  code: string;
  name: string;
  price_cents: number;
  quota_included: Record<string, number>;
  features: Record<string, boolean>;
}

interface DailyUsage {
  api_id: string;
  api_name: string;
  day: string;
  calls: number;
  tokens: number;
}

interface BillingData {
  tenant_id: string;
  month: string;
  plan: PlanSummary;
  daily_usage: DailyUsage[];
  total_calls: number;
  total_tokens: number;
  remaining_calls_today: number;
}

interface PlanInfo {
  code: string;
  name: string;
  description: string | null;
  price_cents: number;
  quota_included: Record<string, number>;
  features: Record<string, boolean> | null;
  sort_order: number;
}

interface ApiUsageRow {
  key: string;
  name: string;
  calls: number;
  tokens: number;
  pct: string;
}

function fmtNum(n: number): string {
  if (n >= 10000) return (n / 10000).toFixed(1) + '万';
  return n.toLocaleString();
}

export function Usage() {
  const [data, setData] = useState<BillingData | null>(null);
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.get<BillingData>('/v1/portal/usage'),
      api.get<PlanInfo[]>('/v1/portal/plans'),
    ])
      .then(([b, p]) => {
        setData(b);
        setPlans(p);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <PageContainer header={{ title: '用量统计' }}>
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Spin />
        </div>
      </PageContainer>
    );
  }
  if (err) {
    return (
      <PageContainer header={{ title: '用量统计' }}>
        <Alert type="error" showIcon message={err} />
      </PageContainer>
    );
  }
  if (!data) return null;

  const dpd = data.plan?.quota_included?.calls_per_day || 0;
  const tpm = data.plan?.quota_included?.tokens_per_month || 0;

  const callsPct = dpd > 0 ? Math.min(100, (data.total_calls / dpd) * 100) : 0;
  const tokensPct = tpm > 0 ? Math.min(100, (data.total_tokens / tpm) * 100) : 0;
  const callsStatus: 'success' | 'active' | 'exception' | 'normal' =
    callsPct >= 100 ? 'exception' : callsPct >= 80 ? 'active' : 'normal';
  const tokensStatus: 'success' | 'active' | 'exception' | 'normal' =
    tokensPct >= 100 ? 'exception' : tokensPct >= 80 ? 'active' : 'normal';

  const apiMap = new Map<string, { calls: number; tokens: number }>();
  data.daily_usage.forEach((u) => {
    const key = u.api_name || u.api_id;
    const prev = apiMap.get(key) || { calls: 0, tokens: 0 };
    apiMap.set(key, {
      calls: prev.calls + u.calls,
      tokens: prev.tokens + u.tokens,
    });
  });
  const apiDetails = Array.from(apiMap.entries()).sort(
    (a, b) => b[1].calls - a[1].calls,
  );
  const apiRows: ApiUsageRow[] = apiDetails.map(([name, u]) => ({
    key: name,
    name,
    calls: u.calls,
    tokens: u.tokens,
    pct:
      data.total_calls > 0
        ? ((u.calls / data.total_calls) * 100).toFixed(0) + '%'
        : '0%',
  }));

  const planColumns: ColumnsType<PlanInfo> = [
    {
      title: 'Plan',
      dataIndex: 'name',
      render: (v: string, r: PlanInfo) =>
        r.code === data.plan.code ? (
          <Typography.Text strong>{v}</Typography.Text>
        ) : (
          v
        ),
    },
    {
      title: '月费',
      dataIndex: 'price_cents',
      align: 'right',
      render: (v: number) => (v > 0 ? `¥${v / 100}/月` : '免费'),
    },
    {
      title: '日调用',
      align: 'right',
      render: (_, r) => fmtNum(r.quota_included.calls_per_day || 0),
    },
    {
      title: 'SDK',
      align: 'right',
      render: (_, r) => (r.features?.sdk ? '✓' : '✗'),
    },
    {
      title: '',
      align: 'right',
      render: (_, r) =>
        r.code === data.plan.code ? <Tag color="blue">当前</Tag> : null,
    },
  ];

  const apiColumns: ColumnsType<ApiUsageRow> = [
    { title: 'API', dataIndex: 'name' },
    {
      title: '调用',
      dataIndex: 'calls',
      align: 'right',
      render: (v: number) => fmtNum(v),
    },
    {
      title: 'Token',
      dataIndex: 'tokens',
      align: 'right',
      render: (v: number) => fmtNum(v),
    },
    { title: '占比', dataIndex: 'pct', align: 'right' },
  ];

  return (
    <PageContainer header={{ title: '用量统计' }}>
      <Typography.Paragraph type="secondary">
        账期：{data.month}
      </Typography.Paragraph>

      <Card style={{ marginBottom: 16 }}>
        <Space style={{ justifyContent: 'space-between', width: '100%' }}>
          <div>
            <Typography.Text type="secondary">当前 Plan</Typography.Text>
            <Typography.Title level={4} style={{ margin: 0 }}>
              {data.plan.name}
            </Typography.Title>
          </div>
          <Button type="link" href="/plans">
            升级 Plan →
          </Button>
        </Space>
      </Card>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={12}>
          <Card>
            <Statistic
              title="调用次数"
              value={fmtNum(data.total_calls)}
              suffix={` / ${fmtNum(dpd)}`}
            />
            <Progress percent={Math.round(callsPct)} status={callsStatus} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              今日剩余：{fmtNum(data.remaining_calls_today)}
            </Typography.Text>
          </Card>
        </Col>
        <Col span={12}>
          <Card>
            <Statistic
              title="Token 消耗"
              value={fmtNum(data.total_tokens)}
              suffix={` / ${fmtNum(tpm)}`}
            />
            <Progress percent={Math.round(tokensPct)} status={tokensStatus} />
          </Card>
        </Col>
      </Row>

      <Card title={`Plan 对比`} style={{ marginBottom: 16 }}>
        <Table<PlanInfo>
          rowKey="code"
          size="small"
          columns={planColumns}
          dataSource={plans}
          pagination={false}
        />
      </Card>

      <Card title="按 API 明细">
        {apiRows.length === 0 ? (
          <Empty description="本月尚无 API 调用记录" />
        ) : (
          <Table<ApiUsageRow>
            rowKey="key"
            size="small"
            columns={apiColumns}
            dataSource={apiRows}
            pagination={false}
          />
        )}
      </Card>
    </PageContainer>
  );
}
