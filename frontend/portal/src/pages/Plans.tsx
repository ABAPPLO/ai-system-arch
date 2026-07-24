import { useEffect, useState } from 'react';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  Button,
  Card,
  Col,
  Row,
  Spin,
  Typography,
  message,
  Modal,
} from 'antd';
import { CheckOutlined, CloseOutlined } from '@ant-design/icons';

import { api } from '../api/client';

interface PlanInfo {
  code: string;
  name: string;
  description: string | null;
  price_cents: number;
  quota_included: Record<string, number>;
  features: Record<string, boolean> | null;
  sort_order: number;
}

const FEAT_LABELS: Record<string, string> = {
  api_catalog: 'API 目录',
  try_it: '在线调试',
  sdk: 'SDK 下载',
};

export function Plans() {
  const [plans, setPlans] = useState<PlanInfo[]>([]);
  const [currentPlan, setCurrentPlan] = useState('');
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  useEffect(() => {
    setLoading(true);
    setErr('');
    Promise.all([
      api.get<PlanInfo[]>('/v1/portal/plans'),
      api.get<{ plan_code: string }>('/v1/portal/subscription'),
    ])
      .then(([p, sub]) => {
        setPlans(p);
        setCurrentPlan(sub.plan_code);
      })
      .catch((e) => setErr(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false));
  }, []);

  const doUpgrade = (code: string, name: string) => {
    Modal.confirm({
      title: '确认升级',
      content: `确认升级到 ${name}？`,
      okText: '确认升级',
      cancelText: '取消',
      onOk: async () => {
        try {
          await api.post('/v1/portal/subscribe', {
            plan_code: code,
            action: 'upgrade',
          });
          setCurrentPlan(code);
          message.success('升级成功');
        } catch (e) {
          message.error(e instanceof Error ? e.message : '升级失败');
        }
      },
    });
  };

  if (loading) {
    return (
      <PageContainer header={{ title: '选择 Plan' }}>
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Spin />
        </div>
      </PageContainer>
    );
  }
  if (err) {
    return (
      <PageContainer header={{ title: '选择 Plan' }}>
        <Alert type="error" showIcon message={err} />
      </PageContainer>
    );
  }

  return (
    <PageContainer header={{ title: '选择 Plan' }}>
      <Typography.Paragraph type="secondary">
        按需选择，随时升级。
      </Typography.Paragraph>
      <Row gutter={[16, 16]}>
        {plans.map((p) => {
          const isCurrent = p.code === currentPlan;
          const q = p.quota_included;
          return (
            <Col key={p.code} xs={24} sm={12} lg={6}>
              <Card
                styles={{ body: { display: 'flex', flexDirection: 'column', minHeight: 320 } }}
                style={
                  isCurrent
                    ? { borderColor: '#1677ff', borderWidth: 2 }
                    : undefined
                }
              >
                <Typography.Title level={4} style={{ marginBottom: 8 }}>
                  {p.name}
                </Typography.Title>
                {p.price_cents > 0 ? (
                  <div style={{ marginBottom: 4 }}>
                    <Typography.Text strong style={{ fontSize: 28 }}>
                      ¥{p.price_cents / 100}
                    </Typography.Text>
                    <Typography.Text type="secondary">/月</Typography.Text>
                  </div>
                ) : (
                  <Typography.Text strong style={{ fontSize: 28 }}>
                    免费
                  </Typography.Text>
                )}
                <Typography.Paragraph
                  type="secondary"
                  style={{ minHeight: 33, marginBottom: 12 }}
                >
                  {p.description || ''}
                </Typography.Paragraph>

                <div style={{ flex: 1, marginBottom: 12 }}>
                  <Typography.Paragraph style={{ marginBottom: 4 }}>
                    📞 {(q.calls_per_day || 0).toLocaleString()} 次/日
                  </Typography.Paragraph>
                  <Typography.Paragraph style={{ marginBottom: 4 }}>
                    🔤 {(q.tokens_per_month || 0).toLocaleString()} Token/月
                  </Typography.Paragraph>
                  {Object.entries(FEAT_LABELS).map(([k, v]) => (
                    <Typography.Paragraph
                      key={k}
                      style={{ marginBottom: 4 }}
                    >
                      {p.features?.[k] ? (
                        <CheckOutlined style={{ color: '#52c41a' }} />
                      ) : (
                        <CloseOutlined style={{ color: '#bfbfbf' }} />
                      )}{' '}
                      {v}
                    </Typography.Paragraph>
                  ))}
                </div>

                <div>
                  {isCurrent ? (
                    <Button block disabled>
                      当前 Plan
                    </Button>
                  ) : p.code === 'enterprise' ? (
                    <Button block href="mailto:sales@apihub.com">
                      联系销售
                    </Button>
                  ) : (
                    <Button
                      type="primary"
                      block
                      onClick={() => doUpgrade(p.code, p.name)}
                    >
                      立即升级
                    </Button>
                  )}
                </div>
              </Card>
            </Col>
          );
        })}
      </Row>
    </PageContainer>
  );
}
