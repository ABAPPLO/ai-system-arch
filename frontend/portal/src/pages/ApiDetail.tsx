import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Empty,
  Input,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';

import { api, ApiError } from '../api/client';

interface VersionItem {
  version_id: string;
  version: string;
  method: string;
  path: string;
  backend_type: string;
  status: string;
  request_schema: Record<string, unknown> | null;
  response_schema: Record<string, unknown> | null;
  ai_streaming?: boolean;
}

interface ApiDetailData {
  api_id: string;
  name: string;
  description: string | null;
  category: string;
  tags: string[];
  base_path: string;
  visibility: string;
  api_status: string;
  versions: VersionItem[];
}

interface TryResponse {
  status: number;
  headers: Record<string, string>;
  body: unknown;
  latency_ms: number;
  error: string | null;
}

interface ExampleResponse {
  curl: string;
  python: string;
  javascript: string;
  notes: string[];
}

type TabKey = 'docs' | 'schema' | 'examples' | 'try';

const BACKEND_TAG: Record<string, { label: string; color: string }> = {
  http: { label: 'HTTP', color: 'blue' },
  ai_model: { label: 'AI SSE', color: 'purple' },
  async_task: { label: 'Async Task', color: 'orange' },
  workflow: { label: 'Workflow', color: 'default' },
};

function StatusTag({ status }: { status: number }) {
  const color =
    status < 300
      ? 'green'
      : status < 400
        ? 'gold'
        : status < 500
          ? 'orange'
          : 'red';
  return (
    <Tag color={color} style={{ fontFamily: 'monospace' }}>
      {status}
    </Tag>
  );
}

interface SchemaRow {
  key: string;
  name: string;
  type: string;
  required: boolean;
  desc: string;
}

function SchemaTable({
  schema,
}: {
  schema: Record<string, unknown> | null;
}) {
  if (!schema || !schema.properties) {
    return <Empty description="无 schema 定义" />;
  }
  const props = schema.properties as Record<string, Record<string, unknown>>;
  const required = (schema.required as string[]) || [];
  const data: SchemaRow[] = Object.entries(props).map(([name, p]) => ({
    key: name,
    name,
    type: String(p.type || 'any'),
    required: required.includes(name),
    desc: String(p.description || ''),
  }));
  const columns: ColumnsType<SchemaRow> = [
    {
      title: '字段',
      dataIndex: 'name',
      render: (v: string) => <Typography.Text code>{v}</Typography.Text>,
    },
    { title: '类型', dataIndex: 'type' },
    {
      title: '必填',
      dataIndex: 'required',
      render: (v: boolean) => (v ? '✓' : ''),
    },
    {
      title: '说明',
      dataIndex: 'desc',
      render: (v: string) => (
        <Typography.Text type="secondary">{v}</Typography.Text>
      ),
    },
  ];
  return (
    <Table<SchemaRow>
      size="small"
      pagination={false}
      columns={columns}
      dataSource={data}
    />
  );
}

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <div style={{ position: 'relative' }}>
      <pre
        style={{
          background: '#0f172a',
          color: '#e2e8f0',
          padding: 16,
          borderRadius: 6,
          overflowX: 'auto',
          fontSize: 13,
          margin: 0,
        }}
      >
        <code>{code}</code>
      </pre>
      <Button
        size="small"
        onClick={onCopy}
        style={{ position: 'absolute', top: 8, right: 8 }}
      >
        {copied ? '已复制' : '复制'}
      </Button>
    </div>
  );
}

export function ApiDetail() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const [detail, setDetail] = useState<ApiDetailData | null>(null);
  const [examples, setExamples] = useState<ExampleResponse | null>(null);
  const [activeTab, setActiveTab] = useState<TabKey>('docs');
  const [selectedVerIdx, setSelectedVerIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Try-it 状态：API Key 必须是明文手动输入（P1 修复语义），不得改成下拉选 app。
  const [selectedKey, setSelectedKey] = useState('');
  const [pathParams, setPathParams] = useState<Record<string, string>>({});
  const [queryParams, setQueryParams] = useState<{ key: string; value: string }[]>(
    [],
  );
  const [bodyText, setBodyText] = useState('');
  const [tryResp, setTryResp] = useState<TryResponse | null>(null);
  const [tryLoading, setTryLoading] = useState(false);
  const [sandbox, setSandbox] = useState(false);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setError('');
    api
      .get<ApiDetailData>(`/v1/portal/apis/${id}`)
      .then((d) => {
        setDetail(d);
        const v = d.versions[0];
        if (v) {
          if (v.request_schema) {
            const example = (v.request_schema as Record<string, unknown>)
              .example;
            setBodyText(example ? JSON.stringify(example, null, 2) : '{\n  \n}');
          }
          const extracted: Record<string, string> = {};
          for (const m of (v.path || '').matchAll(/\{(\w+)\}/g)) {
            extracted[m[1]] = '';
          }
          setPathParams(extracted);
        }
      })
      .catch((err) => setError(err instanceof Error ? err.message : '加载失败'))
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    if (activeTab === 'examples' && id && !examples) {
      api
        .get<ExampleResponse>(`/v1/docs/apis/${id}/examples`)
        .then(setExamples)
        .catch(() => {});
    }
  }, [activeTab, id, examples]);

  const version = detail?.versions[selectedVerIdx] || null;

  const doTry = async () => {
    if (!id || !version || !selectedKey) return;
    setTryLoading(true);
    setTryResp(null);
    let parsedBody: unknown = null;
    if (bodyText.trim()) {
      try {
        parsedBody = JSON.parse(bodyText);
      } catch (e) {
        setTryResp({
          status: 0,
          headers: {},
          body: null,
          latency_ms: 0,
          error:
            '请求体 JSON 解析失败：' +
            (e instanceof Error ? e.message : String(e)),
        });
        setTryLoading(false);
        return;
      }
    }
    try {
      const resp = await api.post<TryResponse>('/v1/portal/try', {
        api_id: id,
        version_id: version.version_id,
        method: version.method,
        path_params: pathParams,
        query_params: Object.fromEntries(
          queryParams.filter((q) => q.key).map((q) => [q.key, q.value]),
        ),
        body: parsedBody,
        api_key: selectedKey,
        environment: sandbox ? 'sandbox' : 'production',
      });
      setTryResp(resp);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      setTryResp({
        status: 0,
        headers: {},
        body: null,
        latency_ms: 0,
        error: msg,
      });
    } finally {
      setTryLoading(false);
    }
  };

  if (loading) {
    return (
      <PageContainer header={{ title: '加载中…' }}>
        <div style={{ textAlign: 'center', padding: 80 }}>
          <Spin />
        </div>
      </PageContainer>
    );
  }

  if (error || !detail) {
    return (
      <PageContainer header={{ title: 'API 详情' }}>
        <Button
          type="link"
          onClick={() => nav('/apis')}
          style={{ paddingLeft: 0, marginBottom: 8 }}
        >
          ← 返回目录
        </Button>
        <Alert type="error" showIcon message={error || 'API 不存在'} />
      </PageContainer>
    );
  }

  const badge = BACKEND_TAG[version?.backend_type || ''] || BACKEND_TAG.http;

  const tabItems = [
    {
      key: 'docs' as const,
      label: '文档说明',
      children: (
        <div>
          <Typography.Paragraph>
            {detail.description || '暂无文档说明'}
          </Typography.Paragraph>
          <Space size={[4, 4]} wrap>
            <Typography.Text type="secondary">标签：</Typography.Text>
            {detail.tags.map((t) => (
              <Tag key={t}>#{t}</Tag>
            ))}
          </Space>
        </div>
      ),
    },
    {
      key: 'schema' as const,
      label: '请求/响应',
      children: (
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <Card title="请求参数">
            <SchemaTable schema={version?.request_schema || null} />
          </Card>
          <Card title="响应参数">
            <SchemaTable schema={version?.response_schema || null} />
          </Card>
        </Space>
      ),
    },
    {
      key: 'examples' as const,
      label: '调用示例',
      children: (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          {examples?.notes.map((n, i) => (
            <Alert key={i} type="warning" showIcon message={n} />
          ))}
          {examples ? (
            <>
              <div>
                <Typography.Title level={5}>curl</Typography.Title>
                <CodeBlock code={examples.curl} />
              </div>
              <div>
                <Typography.Title level={5}>Python</Typography.Title>
                <CodeBlock code={examples.python} />
              </div>
              <div>
                <Typography.Title level={5}>JavaScript</Typography.Title>
                <CodeBlock code={examples.javascript} />
              </div>
            </>
          ) : (
            <Empty description="加载示例中…" />
          )}
        </Space>
      ),
    },
    {
      key: 'try' as const,
      label: '试试',
      children: version ? (
        <Card>
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            {/* API Key —— 明文手动输入（保留 P1 bug 修复语义，不改为下拉选 app） */}
            <div>
              <Typography.Text strong>API Key（明文）</Typography.Text>
              <Input.Password
                autoComplete="off"
                placeholder="粘贴 ak_ 开头的 API Key"
                value={selectedKey}
                onChange={(e) => setSelectedKey(e.target.value)}
                style={{ marginTop: 4, fontFamily: 'monospace' }}
              />
              <Typography.Text
                type="secondary"
                style={{ fontSize: 12, display: 'block', marginTop: 4 }}
              >
                明文 Key 仅创建时显示一次，请到{' '}
                <Button
                  type="link"
                  size="small"
                  style={{ padding: 0 }}
                  onClick={() => nav('/apps')}
                >
                  应用管理
                </Button>{' '}
                创建并复制（服务端只存哈希，无法代为回填）。
              </Typography.Text>
            </div>

            {Object.keys(pathParams).length > 0 && (
              <div>
                <Typography.Text strong>路径参数</Typography.Text>
                <Descriptions column={1} size="small" style={{ marginTop: 8 }}>
                  {Object.entries(pathParams).map(([key, val]) => (
                    <Descriptions.Item
                      key={key}
                      label={<Typography.Text code>{key}</Typography.Text>}
                    >
                      <Input
                        value={val}
                        onChange={(e) =>
                          setPathParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                      />
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              </div>
            )}

            <div>
              <Typography.Text strong>查询参数</Typography.Text>
              <Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
                {queryParams.map((q, i) => (
                  <Space.Compact key={i} style={{ width: '100%' }}>
                    <Input
                      style={{ width: '30%' }}
                      placeholder="key"
                      value={q.key}
                      onChange={(e) => {
                        const next = [...queryParams];
                        next[i] = { ...next[i], key: e.target.value };
                        setQueryParams(next);
                      }}
                    />
                    <Input
                      style={{ width: '60%' }}
                      placeholder="value"
                      value={q.value}
                      onChange={(e) => {
                        const next = [...queryParams];
                        next[i] = { ...next[i], value: e.target.value };
                        setQueryParams(next);
                      }}
                    />
                    <Button
                      danger
                      onClick={() =>
                        setQueryParams(queryParams.filter((_, j) => j !== i))
                      }
                    >
                      删除
                    </Button>
                  </Space.Compact>
                ))}
                <Button
                  type="dashed"
                  onClick={() =>
                    setQueryParams([...queryParams, { key: '', value: '' }])
                  }
                >
                  + Add
                </Button>
              </Space>
            </div>

            <Space>
              <Typography.Text strong>沙箱环境</Typography.Text>
              <Checkbox
                checked={sandbox}
                onChange={(e) => setSandbox(e.target.checked)}
              >
                模拟后端
              </Checkbox>
              {sandbox && <Tag color="orange">沙箱</Tag>}
            </Space>

            <div>
              <Typography.Text strong>请求体（JSON）</Typography.Text>
              <Input.TextArea
                rows={8}
                value={bodyText}
                onChange={(e) => setBodyText(e.target.value)}
                placeholder='{"key": "value"}'
                style={{ marginTop: 4, fontFamily: 'monospace' }}
              />
            </div>

            <Space>
              <Button
                type="primary"
                loading={tryLoading}
                disabled={!selectedKey}
                onClick={doTry}
              >
                ▶ Send
              </Button>
              <Button
                onClick={() => {
                  setTryResp(null);
                  setBodyText('{\n  \n}');
                  setQueryParams([]);
                }}
              >
                Clear
              </Button>
            </Space>

            {tryResp && (
              <Card
                size="small"
                title={
                  <Space>
                    <StatusTag status={tryResp.status} />
                    {tryResp.error && (
                      <Typography.Text type="danger">
                        {tryResp.error}
                      </Typography.Text>
                    )}
                    {!tryResp.error && tryResp.latency_ms > 0 && (
                      <Typography.Text type="secondary">
                        {tryResp.latency_ms}ms
                      </Typography.Text>
                    )}
                  </Space>
                }
              >
                {tryResp.body !== null && tryResp.body !== undefined && (
                  <pre
                    style={{
                      background: '#0f172a',
                      color: '#e2e8f0',
                      padding: 16,
                      borderRadius: 6,
                      overflowX: 'auto',
                      fontSize: 13,
                      margin: 0,
                    }}
                  >
                    <code>{JSON.stringify(tryResp.body, null, 2)}</code>
                  </pre>
                )}
              </Card>
            )}
          </Space>
        </Card>
      ) : (
        <Empty description="该版本无可用端点" />
      ),
    },
  ];

  return (
    <PageContainer header={{ title: detail.name }}>
      <Button
        type="link"
        onClick={() => nav('/apis')}
        style={{ paddingLeft: 0, marginBottom: 8 }}
      >
        ← 返回目录
      </Button>

      <Space style={{ marginBottom: 8 }}>
        <Tag color={badge.color}>{badge.label}</Tag>
        <Tag>{detail.visibility}</Tag>
      </Space>

      <Descriptions column={1} size="small" style={{ marginBottom: 16 }}>
        <Descriptions.Item label="分类">
          {detail.category}
        </Descriptions.Item>
        <Descriptions.Item label="描述">
          {detail.description || '—'}
        </Descriptions.Item>
      </Descriptions>

      {detail.versions.length > 1 && (
        <Space style={{ marginBottom: 16 }}>
          <Typography.Text>版本：</Typography.Text>
          <Select
            value={selectedVerIdx}
            onChange={(idx) => {
              setSelectedVerIdx(idx);
              const v = detail.versions[idx];
              const ex: Record<string, string> = {};
              for (const m of (v?.path || '').matchAll(/\{(\w+)\}/g)) {
                ex[m[1]] = '';
              }
              setPathParams(ex);
              if (v?.request_schema) {
                const example = (v.request_schema as Record<string, unknown>)
                  .example;
                setBodyText(example ? JSON.stringify(example, null, 2) : '{\n  \n}');
              }
            }}
            options={detail.versions.map((v, i) => ({
              label: `${v.version} (${v.status})`,
              value: i,
            }))}
            style={{ width: 200 }}
          />
          {version && (
            <Typography.Text code>
              {version.method} {version.path}
            </Typography.Text>
          )}
        </Space>
      )}

      <Tabs
        activeKey={activeTab}
        onChange={(k) => setActiveTab(k as TabKey)}
        items={tabItems}
      />
    </PageContainer>
  );
}
