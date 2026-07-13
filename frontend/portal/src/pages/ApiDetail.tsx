import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
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

interface AppItem {
  id: string;
  name: string;
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

type Tab = 'docs' | 'schema' | 'examples' | 'try';

const BACKEND_BADGE: Record<string, { label: string; color: string }> = {
  http:       { label: 'HTTP',       color: 'bg-blue-100 text-blue-700' },
  ai_model:   { label: 'AI SSE',     color: 'bg-purple-100 text-purple-700' },
  async_task: { label: 'Async Task', color: 'bg-orange-100 text-orange-700' },
  workflow:   { label: 'Workflow',   color: 'bg-gray-100 text-gray-600' },
};

const TAB_LABEL: Record<Tab, string> = {
  docs: '文档说明',
  schema: '请求/响应',
  examples: '调用示例',
  try: '试试',
};

function StatusBadge({ status }: { status: number }) {
  const color =
    status < 300 ? 'bg-green-100 text-green-700' :
    status < 400 ? 'bg-yellow-100 text-yellow-700' :
    status < 500 ? 'bg-orange-100 text-orange-700' :
    'bg-red-100 text-red-700';
  return <span className={`font-mono text-sm px-2 py-0.5 rounded ${color}`}>{status}</span>;
}

function SchemaTable({ schema }: { schema: Record<string, unknown> | null }) {
  if (!schema || !schema.properties) {
    return <p className="text-gray-400 text-sm">无 schema 定义</p>;
  }
  const props = schema.properties as Record<string, unknown>;
  const required = (schema.required as string[]) || [];
  return (
    <table className="w-full text-sm border-collapse">
      <thead>
        <tr className="border-b bg-gray-50">
          <th className="text-left px-3 py-2">字段</th>
          <th className="text-left px-3 py-2">类型</th>
          <th className="text-left px-3 py-2">必填</th>
          <th className="text-left px-3 py-2">说明</th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(props).map(([name, prop]) => {
          const p = prop as Record<string, unknown>;
          return (
            <tr key={name} className="border-b">
              <td className="px-3 py-1.5 font-mono">{name}</td>
              <td className="px-3 py-1.5">{String(p.type || 'any')}</td>
              <td className="px-3 py-1.5">{required.includes(name) ? '✓' : ''}</td>
              <td className="px-3 py-1.5 text-gray-500">{String(p.description || '')}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      <pre className="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-x-auto">
        <code>{code}</code>
      </pre>
      <button
        className="absolute top-2 right-2 text-xs bg-gray-700 px-2 py-1 rounded text-gray-300 hover:bg-gray-600"
        onClick={() => { navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
      >
        {copied ? '已复制' : '复制'}
      </button>
    </div>
  );
}

export function ApiDetail() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const [detail, setDetail] = useState<ApiDetailData | null>(null);
  const [examples, setExamples] = useState<ExampleResponse | null>(null);
  const [apps, setApps] = useState<AppItem[]>([]);
  const [activeTab, setActiveTab] = useState<Tab>('docs');
  const [selectedVerIdx, setSelectedVerIdx] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [selectedKey, setSelectedKey] = useState('');
  const [pathParams, setPathParams] = useState<Record<string, string>>({});
  const [queryParams, setQueryParams] = useState<{ key: string; value: string }[]>([]);
  const [bodyText, setBodyText] = useState('');
  const [tryResp, setTryResp] = useState<TryResponse | null>(null);
  const [tryLoading, setTryLoading] = useState(false);
  const [sandbox, setSandbox] = useState(false);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setError('');
    Promise.all([
      api.get<ApiDetailData>(`/v1/portal/apis/${id}`),
      api.get<AppItem[]>('/v1/portal/apps').catch(() => [] as AppItem[]),
    ])
      .then(([d, a]) => {
        setDetail(d);
        setApps(a);
        const v = d.versions[0];
        if (v) {
          if (v.request_schema) {
            const example = (v.request_schema as Record<string, unknown>).example;
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
      api.get<ExampleResponse>(`/v1/docs/apis/${id}/examples`)
        .then(setExamples)
        .catch(() => {});
    }
  }, [activeTab, id, examples]);

  const version = detail?.versions[selectedVerIdx] || null;

  const doTry = async () => {
    if (!id || !version || !selectedKey) return;
    setTryLoading(true);
    setTryResp(null);
    try {
      const resp = await api.post<TryResponse>('/v1/portal/try', {
        api_id: id,
        version_id: version.version_id,
        method: version.method,
        path_params: pathParams,
        query_params: Object.fromEntries(
          queryParams.filter((q) => q.key).map((q) => [q.key, q.value]),
        ),
        body: bodyText ? JSON.parse(bodyText) : null,
        api_key: selectedKey,
        environment: sandbox ? 'sandbox' : 'production',
      });
      setTryResp(resp);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      setTryResp({ status: 0, headers: {}, body: null, latency_ms: 0, error: msg });
    } finally {
      setTryLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="max-w-4xl mx-auto p-4">
        <button onClick={() => nav('/apis')} className="text-blue-600 mb-4">&larr; 返回目录</button>
        <div className="bg-red-50 border border-red-200 rounded p-4 text-red-700">
          {error || 'API 不存在'}
        </div>
      </div>
    );
  }

  const badge = BACKEND_BADGE[version?.backend_type || ''] || BACKEND_BADGE.http;

  return (
    <div className="max-w-4xl mx-auto p-4">
      <button onClick={() => nav('/apis')} className="text-blue-600 mb-2">&larr; 返回目录</button>

      <div className="flex items-center justify-between mb-2">
        <h1 className="text-2xl font-bold">{detail.name}</h1>
        <div className="flex items-center gap-2">
          <span className={`text-xs font-medium px-2 py-0.5 rounded ${badge.color}`}>{badge.label}</span>
          <span className="text-xs text-gray-400">{detail.visibility}</span>
        </div>
      </div>
      <p className="text-gray-500 text-sm mb-1">分类: {detail.category}</p>
      <p className="text-gray-600 mb-4">{detail.description || ''}</p>

      {detail.versions.length > 1 && (
        <div className="mb-4 flex items-center gap-2">
          <label className="text-sm text-gray-500">版本:</label>
          <select
            className="border rounded px-3 py-1 text-sm"
            value={selectedVerIdx}
            onChange={(e) => {
              const idx = parseInt(e.target.value, 10);
              setSelectedVerIdx(idx);
              const v = detail.versions[idx];
              const ex: Record<string, string> = {};
              for (const m of (v?.path || '').matchAll(/\{(\w+)\}/g)) {
                ex[m[1]] = '';
              }
              setPathParams(ex);
              if (v?.request_schema) {
                const example = (v.request_schema as Record<string, unknown>).example;
                setBodyText(example ? JSON.stringify(example, null, 2) : '{\n  \n}');
              }
            }}
          >
            {detail.versions.map((v, i) => (
              <option key={v.version_id} value={i}>
                {v.version} ({v.status})
              </option>
            ))}
          </select>
          {version && (
            <span className="text-sm text-gray-500">{version.method} {version.path}</span>
          )}
        </div>
      )}

      <div className="flex border-b mb-4">
        {(['docs', 'schema', 'examples', 'try'] as Tab[]).map((tab) => (
          <button
            key={tab}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${
              activeTab === tab
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700'
            }`}
            onClick={() => setActiveTab(tab)}
          >
            {TAB_LABEL[tab]}
          </button>
        ))}
      </div>

      {activeTab === 'docs' && (
        <div>
          <p>{detail.description || '暂无文档说明'}</p>
          <div className="mt-2">
            <span className="text-sm text-gray-500">标签: </span>
            {detail.tags.map((t) => (
              <span key={t} className="bg-gray-100 text-sm px-2 py-0.5 rounded mr-1">#{t}</span>
            ))}
          </div>
        </div>
      )}

      {activeTab === 'schema' && (
        <div className="space-y-6">
          <div>
            <h3 className="font-semibold text-sm mb-2">请求参数</h3>
            <SchemaTable schema={version?.request_schema || null} />
          </div>
          <div>
            <h3 className="font-semibold text-sm mb-2">响应参数</h3>
            <SchemaTable schema={version?.response_schema || null} />
          </div>
        </div>
      )}

      {activeTab === 'examples' && (
        <div className="space-y-4">
          {examples?.notes.map((n, i) => (
            <p key={i} className="text-yellow-700 bg-yellow-50 p-2 rounded text-sm">{n}</p>
          ))}
          {examples ? (
            <>
              <div>
                <h3 className="font-semibold text-sm mb-1">curl</h3>
                <CodeBlock code={examples.curl} />
              </div>
              <div>
                <h3 className="font-semibold text-sm mb-1">Python</h3>
                <CodeBlock code={examples.python} />
              </div>
              <div>
                <h3 className="font-semibold text-sm mb-1">JavaScript</h3>
                <CodeBlock code={examples.javascript} />
              </div>
            </>
          ) : (
            <p className="text-gray-400 text-sm">加载示例中…</p>
          )}
        </div>
      )}

      {activeTab === 'try' && version && (
        <div className="border rounded-lg p-4 space-y-4">
          <div>
            <label className="text-sm font-semibold">API Key</label>
            <select
              className="w-full border rounded px-3 py-2 mt-1"
              value={selectedKey}
              onChange={(e) => setSelectedKey(e.target.value)}
            >
              <option value="">-- 请选择 Key --</option>
              {apps.map((app) => (
                <option key={app.id} value={app.id}>{app.name}</option>
              ))}
            </select>
            {apps.length === 0 && (
              <p className="text-xs text-orange-600 mt-1">
                请先在「应用管理」中创建应用和 API Key
              </p>
            )}
          </div>

          {Object.keys(pathParams).length > 0 && (
            <div>
              <label className="text-sm font-semibold">路径参数</label>
              {Object.entries(pathParams).map(([key, val]) => (
                <div key={key} className="flex items-center gap-2 mt-1">
                  <span className="text-sm font-mono text-gray-500 w-24">{key}</span>
                  <input
                    className="flex-1 border rounded px-3 py-1.5 text-sm"
                    value={val}
                    onChange={(e) => setPathParams((prev) => ({ ...prev, [key]: e.target.value }))}
                  />
                </div>
              ))}
            </div>
          )}

          <div>
            <label className="text-sm font-semibold">查询参数</label>
            {queryParams.map((q, i) => (
              <div key={i} className="flex items-center gap-2 mt-1">
                <input
                  className="border rounded px-2 py-1 text-sm w-32"
                  placeholder="key"
                  value={q.key}
                  onChange={(e) => {
                    const next = [...queryParams];
                    next[i] = { ...next[i], key: e.target.value };
                    setQueryParams(next);
                  }}
                />
                <input
                  className="border rounded px-2 py-1 text-sm flex-1"
                  placeholder="value"
                  value={q.value}
                  onChange={(e) => {
                    const next = [...queryParams];
                    next[i] = { ...next[i], value: e.target.value };
                    setQueryParams(next);
                  }}
                />
                <button
                  className="text-red-500 text-sm"
                  onClick={() => setQueryParams(queryParams.filter((_, j) => j !== i))}
                >
                  删除
                </button>
              </div>
            ))}
            <button
              className="text-blue-600 text-sm mt-1"
              onClick={() => setQueryParams([...queryParams, { key: '', value: '' }])}
            >
              + Add
            </button>
          </div>

          <div className="flex items-center gap-2">
            <label className="text-sm font-semibold">沙箱环境</label>
            <button
              className={`relative w-10 h-5 rounded-full transition-colors ${sandbox ? 'bg-blue-500' : 'bg-gray-300'}`}
              onClick={() => setSandbox(!sandbox)}
            >
              <span className={`block w-4 h-4 bg-white rounded-full transition-transform ${sandbox ? 'translate-x-5' : 'translate-x-0.5'}`} />
            </button>
            {sandbox && <span className="text-xs text-orange-600">模拟后端</span>}
          </div>

          <div>
            <label className="text-sm font-semibold">请求体 (JSON)</label>
            <textarea
              className="w-full border rounded px-3 py-2 text-sm font-mono mt-1"
              rows={8}
              value={bodyText}
              onChange={(e) => setBodyText(e.target.value)}
              placeholder='{"key": "value"}'
            />
          </div>

          <button
            className="bg-blue-600 text-white px-6 py-2 rounded font-medium disabled:opacity-50"
            disabled={tryLoading || !selectedKey}
            onClick={doTry}
          >
            {tryLoading ? '发送中…' : '▶ Send'}
          </button>

          <button
            className="ml-2 px-4 py-2 border rounded"
            onClick={() => {
              setTryResp(null);
              setBodyText('{\n  \n}');
              setQueryParams([]);
            }}
          >
            Clear
          </button>

          {tryResp && (
            <div className="border rounded p-4 bg-gray-50">
              <div className="flex items-center gap-2 mb-2">
                <StatusBadge status={tryResp.status} />
                {tryResp.error && <span className="text-red-600 text-sm">{tryResp.error}</span>}
                {!tryResp.error && tryResp.latency_ms > 0 && (
                  <span className="text-gray-400 text-sm">{tryResp.latency_ms}ms</span>
                )}
              </div>
              {tryResp.body !== null && tryResp.body !== undefined && (
                <pre className="bg-gray-900 text-gray-100 p-4 rounded text-sm overflow-x-auto">
                  <code>{JSON.stringify(tryResp.body, null, 2)}</code>
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
