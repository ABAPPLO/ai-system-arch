import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { PageContainer } from '@ant-design/pro-components';
import {
  Alert,
  Card,
  Col,
  Empty,
  Input,
  Pagination,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import dayjs from 'dayjs';

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

const BACKEND_TAG: Record<BackendType, { label: string; color: string }> = {
  http: { label: 'HTTP', color: 'blue' },
  ai_model: { label: 'AI SSE', color: 'purple' },
  async_task: { label: 'Async Task', color: 'orange' },
  workflow: { label: 'Workflow', color: 'default' },
};

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
    void fetchApis();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, category, tag, offset]);

  const updateParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    if (key !== 'offset') next.delete('offset');
    setParams(next);
  };

  const onSearchChange = (value: string) => {
    setSearchInput(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => updateParam('search', value), 300);
  };

  return (
    <PageContainer header={{ title: 'API 目录' }}>
      <Card style={{ marginBottom: 16 }}>
        <Space wrap>
          <Input.Search
            placeholder="搜索 API 名称或描述…（回车搜索）"
            allowClear
            value={searchInput}
            onChange={(e) => onSearchChange(e.target.value)}
            onSearch={(v) => updateParam('search', v)}
            style={{ width: 320 }}
          />
          <Select
            placeholder="全部分类"
            allowClear
            value={category || undefined}
            onChange={(v) => updateParam('category', v || '')}
            style={{ width: 180 }}
            options={(data?.categories || []).map((c) => ({ label: c, value: c }))}
          />
          <Select
            placeholder="全部标签"
            allowClear
            value={tag || undefined}
            onChange={(v) => updateParam('tag', v || '')}
            style={{ width: 180 }}
            options={(data?.tags || []).map((t) => ({ label: t, value: t }))}
          />
        </Space>
      </Card>

      {loading && (
        <Card>
          <div style={{ textAlign: 'center', padding: 40 }}>
            <Spin />
          </div>
        </Card>
      )}

      {!loading && error && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 16 }}
        />
      )}

      {!loading && !error && data && data.items.length === 0 && (
        <Card>
          <Empty description="没有找到匹配的 API，试试其他关键词" />
        </Card>
      )}

      {!loading && !error && data && data.items.length > 0 && (
        <>
          <Row gutter={[16, 16]}>
            {data.items.map((apiItem) => {
              const badge = BACKEND_TAG[apiItem.backend_type] || BACKEND_TAG.http;
              return (
                <Col key={apiItem.api_id} xs={24} md={12} lg={8}>
                  <Card
                    hoverable
                    onClick={() => nav(`/apis/${apiItem.api_id}`)}
                    title={
                      <Space>
                        <Typography.Text strong>{apiItem.name}</Typography.Text>
                        <Tag color={badge.color}>{badge.label}</Tag>
                      </Space>
                    }
                  >
                    <Typography.Paragraph
                      type="secondary"
                      ellipsis={{ rows: 2 }}
                      style={{ marginBottom: 8, minHeight: 22 }}
                    >
                      {apiItem.description || '—'}
                    </Typography.Paragraph>
                    <Space size={[4, 4]} wrap style={{ marginBottom: 8 }}>
                      {apiItem.tags.map((t) => (
                        <Tag key={t}>#{t}</Tag>
                      ))}
                    </Space>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {apiItem.version} ·{' '}
                      {dayjs(apiItem.updated_at).format('YYYY-MM-DD HH:mm')} 更新
                    </Typography.Text>
                  </Card>
                </Col>
              );
            })}
          </Row>

          {data.total > limit && (
            <div style={{ textAlign: 'center', marginTop: 24 }}>
              <Pagination
                current={Math.floor(offset / limit) + 1}
                pageSize={limit}
                total={data.total}
                showSizeChanger={false}
                onChange={(page) => updateParam('offset', String((page - 1) * limit))}
              />
            </div>
          )}
        </>
      )}
    </PageContainer>
  );
}
