import { useEffect, useState, useRef } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';

type BackendType = 'http' | 'ai_model' | 'async_task' | 'workflow';

interface ApiItem {
  api_id: string;
  name: string;
  description: string | null;
  category: string;
  tags: string[];
  base_path: string;
  visibility: string;
  backend_type: BackendType;
  version: string;
  updated_at: string;
}

interface ApiListResponse {
  items: ApiItem[];
  total: number;
  limit: number;
  offset: number;
  categories: string[];
  tags: string[];
}

const BACKEND_BADGE: Record<BackendType, { label: string; color: string }> = {
  http:       { label: 'HTTP',       color: 'bg-blue-100 text-blue-700' },
  ai_model:   { label: 'AI SSE',     color: 'bg-purple-100 text-purple-700' },
  async_task: { label: 'Async Task', color: 'bg-orange-100 text-orange-700' },
  workflow:   { label: 'Workflow',   color: 'bg-gray-100 text-gray-600' },
};

function timeAgo(iso: string): string {
  if (!iso) return '';
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (sec < 60) return '刚刚';
  if (sec < 3600) return `${Math.floor(sec / 60)}分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}小时前`;
  return `${Math.floor(sec / 86400)}天前`;
}

export function ApiCatalog() {
  const nav = useNavigate();
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<ApiListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [searchInput, setSearchInput] = useState(params.get('search') || '');
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  const search = params.get('search') || '';
  const category = params.get('category') || '';
  const tag = params.get('tag') || '';
  const offset = parseInt(params.get('offset') || '0', 10);
  const limit = 50;

  const fetchApis = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.get<ApiListResponse>('/v1/portal/apis', {
        search: search || undefined,
        category: category || undefined,
        tag: tag || undefined,
        limit,
        offset,
      });
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchApis();
  }, [search, category, tag, offset]);

  const updateParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) { next.set(key, value); } else { next.delete(key); }
    if (key !== 'offset') next.delete('offset');
    setParams(next);
  };

  const doSearch = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    updateParam('search', searchInput);
  };

  const onSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setSearchInput(e.target.value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      updateParam('search', e.target.value);
    }, 300);
  };

  const pageCount = data ? Math.ceil(data.total / limit) : 0;
  const currentPage = Math.floor(offset / limit) + 1;

  return (
    <div className="max-w-4xl mx-auto p-4">
      <h1 className="text-2xl font-bold mb-4">API 目录</h1>

      <div className="flex gap-2 mb-4">
        <input
          className="flex-1 border rounded px-3 py-2"
          placeholder="搜索 API 名称或描述…（回车搜索）"
          value={searchInput}
          onChange={onSearchChange}
          onKeyDown={(e) => e.key === 'Enter' && doSearch()}
        />
        <select
          className="border rounded px-3 py-2"
          value={category}
          onChange={(e) => updateParam('category', e.target.value)}
        >
          <option value="">全部分类</option>
          {(data?.categories || []).map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <select
          className="border rounded px-3 py-2"
          value={tag}
          onChange={(e) => updateParam('tag', e.target.value)}
        >
          <option value="">全部标签</option>
          {(data?.tags || []).map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </div>

      {loading && (
        <div className="flex justify-center py-8">
          <div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" />
        </div>
      )}

      {error && !loading && (
        <div className="bg-red-50 border border-red-200 rounded p-4 text-red-700">{error}</div>
      )}

      {!loading && !error && data && data.items.length === 0 && (
        <div className="text-center py-12 text-gray-500">
          <p className="text-lg">没有找到匹配的 API</p>
          <p className="text-sm">试试其他关键词</p>
        </div>
      )}

      {!loading && data && data.items.length > 0 && (
        <>
          <div className="space-y-3">
            {data.items.map((apiItem) => {
              const badge = BACKEND_BADGE[apiItem.backend_type] || BACKEND_BADGE.http;
              return (
                <div
                  key={apiItem.api_id}
                  className="border rounded-lg p-4 cursor-pointer hover:shadow-md transition-shadow"
                  onClick={() => nav(`/apis/${apiItem.api_id}`)}
                >
                  <div className="flex items-center justify-between mb-1">
                    <h3 className="text-lg font-semibold">{apiItem.name}</h3>
                    <span className={`text-xs font-medium px-2 py-0.5 rounded ${badge.color}`}>
                      {badge.label}
                    </span>
                  </div>
                  <p className="text-gray-600 text-sm mb-2">{apiItem.description || ''}</p>
                  <div className="flex items-center gap-2 text-xs text-gray-400">
                    {apiItem.tags.map((t) => (
                      <span key={t} className="bg-gray-100 px-1.5 py-0.5 rounded">#{t}</span>
                    ))}
                    <span className="ml-auto">{apiItem.version}</span>
                    <span>·</span>
                    <span>{timeAgo(apiItem.updated_at)}更新</span>
                  </div>
                </div>
              );
            })}
          </div>

          {pageCount > 1 && (
            <div className="flex justify-center items-center gap-2 mt-6">
              <button
                className="px-3 py-1 border rounded disabled:opacity-50"
                disabled={offset === 0}
                onClick={() => updateParam('offset', String(offset - limit))}
              >
                上一页
              </button>
              {Array.from({ length: Math.min(pageCount, 10) }, (_, i) => (
                <button
                  key={i}
                  className={`px-3 py-1 border rounded ${currentPage === i + 1 ? 'bg-blue-500 text-white' : ''}`}
                  onClick={() => updateParam('offset', String(i * limit))}
                >
                  {i + 1}
                </button>
              ))}
              {pageCount > 10 && <span>…</span>}
              <button
                className="px-3 py-1 border rounded disabled:opacity-50"
                disabled={offset + limit >= data.total}
                onClick={() => updateParam('offset', String(offset + limit))}
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
