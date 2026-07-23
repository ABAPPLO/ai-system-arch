import { useEffect, useMemo, useState } from 'react';
import { PageContainer } from '@ant-design/pro-components';
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Input,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import { EyeOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import useSWR from 'swr';

import { api } from '../api/client';
import type { ApiListItem } from '../api/types';

const STATUS_COLOR: Record<string, string> = {
  draft: 'default',
  reviewing: 'processing',
  published: 'green',
  deprecated: 'orange',
  retired: 'red',
};

export default function Apis() {
  const [items, setItems] = useState<ApiListItem[]>([]);
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(false);
  const [drawerId, setDrawerId] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      const r = await api.get<{ items: ApiListItem[] }>(
        '/api/registry/v1/apis',
        { limit: 200, offset: 0 },
      );
      setItems(r.items ?? []);
    } catch (e) {
      console.error(e);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const filtered = useMemo(() => {
    const k = keyword.trim().toLowerCase();
    if (!k) return items;
    return items.filter(
      (i) =>
        i.name.toLowerCase().includes(k) || i.id.toLowerCase().includes(k),
    );
  }, [keyword, items]);

  const columns: ColumnsType<ApiListItem> = [
    { title: 'ID', dataIndex: 'id', width: 180, ellipsis: true },
    { title: '名称', dataIndex: 'name' },
    { title: '分类', dataIndex: 'category', width: 120 },
    { title: 'base_path', dataIndex: 'base_path', width: 160 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (v) => <Tag color={STATUS_COLOR[v] ?? 'default'}>{v}</Tag>,
    },
    {
      title: '标签',
      dataIndex: 'tags',
      width: 180,
      render: (v) => {
        const arr = v as string[] | null;
        return arr && arr.length ? (
          <>
            {arr.map((t) => (
              <Tag key={t}>{t}</Tag>
            ))}
          </>
        ) : (
          '—'
        );
      },
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      render: (v) => dayjs(v).format('MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 90,
      fixed: 'right',
      render: (_, r) => (
        <Button
          size="small"
          icon={<EyeOutlined />}
          onClick={() => setDrawerId(r.id)}
        >
          详情
        </Button>
      ),
    },
  ];

  return (
    <PageContainer header={{ title: '接口管理（只读）' }}>
      <Card>
        <Space style={{ marginBottom: 16 }}>
          <Input.Search
            placeholder="按名称或 ID 搜索（当前加载范围）..."
            allowClear
            style={{ width: 320 }}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <Button onClick={() => void load()}>刷新</Button>
        </Space>
        <Table<ApiListItem>
          rowKey="id"
          columns={columns}
          dataSource={filtered}
          loading={loading}
          pagination={{ pageSize: 20 }}
          locale={{ emptyText: loading ? <Spin /> : '暂无数据' }}
          scroll={{ x: 1100 }}
        />
      </Card>
      <ApiDrawer id={drawerId} onClose={() => setDrawerId(null)} />
    </PageContainer>
  );
}

function ApiDrawer({
  id,
  onClose,
}: {
  id: string | null;
  onClose: () => void;
}) {
  const { data, error, isLoading } = useSWR<ApiListItem>(
    id ? `/api/registry/v1/apis/${id}` : null,
    (url: string) => api.get<ApiListItem>(url),
  );

  return (
    <Drawer
      open={id !== null}
      onClose={onClose}
      width={720}
      title={data ? data.name : '加载中...'}
    >
      {isLoading && <div>加载中...</div>}
      {error && <div>加载失败：{(error as Error).message}</div>}
      {data && (
        <Descriptions column={1} size="small" bordered>
          <Descriptions.Item label="ID">
            <Typography.Text code>{data.id}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="名称">{data.name}</Descriptions.Item>
          <Descriptions.Item label="分类">{data.category}</Descriptions.Item>
          <Descriptions.Item label="base_path">
            <Typography.Text code>{data.base_path}</Typography.Text>
          </Descriptions.Item>
          <Descriptions.Item label="状态">
            <Tag color={STATUS_COLOR[data.status] ?? 'default'}>
              {data.status}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="描述">
            {data.description || '—'}
          </Descriptions.Item>
          <Descriptions.Item label="标签">
            {data.tags && data.tags.length ? data.tags.join(', ') : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="租户">{data.tenant_id}</Descriptions.Item>
          <Descriptions.Item label="创建时间">
            {dayjs(data.created_at).format('YYYY-MM-DD HH:mm:ss')}
          </Descriptions.Item>
          <Descriptions.Item label="更新时间">
            {dayjs(data.updated_at).format('YYYY-MM-DD HH:mm:ss')}
          </Descriptions.Item>
        </Descriptions>
      )}
    </Drawer>
  );
}
