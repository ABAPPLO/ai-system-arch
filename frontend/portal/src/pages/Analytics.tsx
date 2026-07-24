import { useEffect, useState } from 'react';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  Card,
  Empty,
  Progress,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { api } from '../api/client';

interface FunnelStep {
  api_id: string;
  path: string;
}
interface FunnelItem {
  trace_id: string;
  step_count: number;
  steps: FunnelStep[];
}
interface CoOccurrenceItem {
  api_a: string;
  path_a: string;
  api_b: string;
  path_b: string;
  pair_count: number;
}

export function Analytics() {
  const [funnel, setFunnel] = useState<FunnelItem[]>([]);
  const [cooccur, setCooccur] = useState<CoOccurrenceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.get<FunnelItem[]>('/v1/portal/analytics/funnel'),
      api.get<CoOccurrenceItem[]>('/v1/portal/analytics/co-occurrence'),
    ])
      .then(([f, c]) => {
        setFunnel(f);
        setCooccur(c);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  const stepDist = funnel.reduce<Record<number, number>>((acc, f) => {
    acc[f.step_count] = (acc[f.step_count] || 0) + 1;
    return acc;
  }, {});
  const distEntries = Object.entries(stepDist).sort(
    ([a], [b]) => Number(a) - Number(b),
  );
  const maxDist = Math.max(...Object.values(stepDist), 1);

  const coColumns: ColumnsType<CoOccurrenceItem> = [
    {
      title: 'API A',
      dataIndex: 'path_a',
      render: (_, r) => <Tag color="blue">{r.path_a || r.api_a}</Tag>,
    },
    {
      title: 'API B',
      dataIndex: 'path_b',
      render: (_, r) => <Tag color="green">{r.path_b || r.api_b}</Tag>,
    },
    {
      title: '共现次数',
      dataIndex: 'pair_count',
      width: 120,
      align: 'right',
      sorter: (a, b) => a.pair_count - b.pair_count,
      defaultSortOrder: 'descend',
    },
  ];

  return (
    <PageContainer
      header={{ title: '高级分析', subTitle: 'API 调用行为路径与模式分析' }}
    >
      {err && (
        <Alert
          type="error"
          showIcon
          message={err}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card
        title="调用链步数分布"
        size="small"
        loading={loading}
        style={{ marginBottom: 16 }}
        extra={
          <Typography.Text type="secondary">
            单个 trace 中的 API 调用次数
          </Typography.Text>
        }
      >
        {distEntries.length === 0 ? (
          <Empty description="暂无数据" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size="middle">
            {distEntries.map(([steps, count]) => (
              <div
                key={steps}
                style={{ display: 'flex', alignItems: 'center', gap: 12 }}
              >
                <Typography.Text style={{ width: 64, textAlign: 'right' }}>
                  {steps} 步
                </Typography.Text>
                <Progress
                  percent={(count / maxDist) * 100}
                  showInfo={false}
                  strokeColor="#1677ff"
                  style={{ flex: 1, margin: 0 }}
                />
                <Typography.Text
                  type="secondary"
                  style={{ width: 90 }}
                >
                  {count} trace
                </Typography.Text>
              </div>
            ))}
          </Space>
        )}
      </Card>

      <Card
        title="最近调用序列"
        size="small"
        loading={loading}
        style={{ marginBottom: 16 }}
      >
        {funnel.length === 0 ? (
          <Empty description="暂无数据" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size="small">
            {funnel.slice(0, 10).map((f) => (
              <div key={f.trace_id}>
                <Typography.Text type="secondary" code style={{ fontSize: 12 }}>
                  {f.trace_id.slice(0, 16)}...
                </Typography.Text>
                <div style={{ marginTop: 4 }}>
                  <Space size={[4, 4]} wrap>
                    {f.steps.map((s, i) => (
                      <Space key={i} size={2}>
                        <Tag color="blue">{s.path || s.api_id}</Tag>
                        {i < f.steps.length - 1 && (
                          <Typography.Text type="secondary">→</Typography.Text>
                        )}
                      </Space>
                    ))}
                  </Space>
                </div>
              </div>
            ))}
          </Space>
        )}
      </Card>

      <Card
        title="API 共现"
        size="small"
        loading={loading}
        extra={
          <Typography.Text type="secondary">
            同一 trace 中的 API 对
          </Typography.Text>
        }
      >
        {cooccur.length === 0 ? (
          <Empty description="暂无数据（需至少 3 对共现）" />
        ) : (
          <Table<CoOccurrenceItem>
            rowKey={(r) => `${r.api_a}|${r.api_b}`}
            size="small"
            columns={coColumns}
            dataSource={cooccur.slice(0, 20)}
            pagination={false}
          />
        )}
      </Card>
    </PageContainer>
  );
}
