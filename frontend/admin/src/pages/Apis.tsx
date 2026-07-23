import { useEffect, useMemo, useState } from 'react';
import {
  ModalForm,
  PageContainer,
  ProFormSelect,
  ProFormSwitch,
  ProFormText,
} from '@ant-design/pro-components';
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Input,
  message,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import { EyeOutlined, PlusOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import useSWR from 'swr';

import { api } from '../api/client';
import type {
  ApiCreateBody,
  ApiListItem,
  ApiVersion,
  ApiVersionCreateBody,
} from '../api/types';

const STATUS_COLOR: Record<string, string> = {
  draft: 'default',
  reviewing: 'processing',
  published: 'green',
  deprecated: 'orange',
  retired: 'red',
};

const BACKEND_TYPE_OPTIONS = [
  { label: 'http', value: 'http' },
  { label: 'async_task', value: 'async_task' },
  { label: 'workflow', value: 'workflow' },
  { label: 'ai_model', value: 'ai_model' },
];
const METHOD_OPTIONS = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'].map((m) => ({
  label: m,
  value: m,
}));

const REGISTRY = '/api/registry/v1';

export default function Apis() {
  const [items, setItems] = useState<ApiListItem[]>([]);
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(false);
  const [drawerId, setDrawerId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const r = await api.get<{ items: ApiListItem[] }>(
        `${REGISTRY}/apis`,
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
    <PageContainer header={{ title: '接口管理' }}>
      <Card>
        <Space style={{ marginBottom: 16 }}>
          <Input.Search
            placeholder="按名称或 ID 搜索（当前加载范围）..."
            allowClear
            style={{ width: 320 }}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <Button onClick={() => void load()}>刷新</Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            新建 API
          </Button>
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
      <ApiDrawer id={drawerId} onClose={() => setDrawerId(null)} onReload={load} />

      <ModalForm<ApiCreateBody>
        title="新建 API"
        width={560}
        open={createOpen}
        onOpenChange={setCreateOpen}
        modalProps={{ destroyOnClose: true }}
        onFinish={async (values) => {
          try {
            await api.post(`${REGISTRY}/apis`, values);
            message.success('API 已创建（draft）');
            void load();
            return true;
          } catch (e) {
            message.error((e as Error).message);
            return false;
          }
        }}
      >
        <ProFormText name="name" label="名称" required />
        <ProFormText name="description" label="描述" />
        <ProFormText
          name="category"
          label="分类"
          required
          placeholder="如 payment / user"
        />
        <ProFormText
          name="base_path"
          label="base_path"
          required
          placeholder="/payment"
          rules={[{ pattern: /^\/[a-z0-9-]+/, message: '以 / 开头，小写字母/数字/连字符' }]}
        />
        <ProFormText name="tags" label="标签（逗号分隔）" />
      </ModalForm>
    </PageContainer>
  );
}

function ApiDrawer({
  id,
  onClose,
  onReload,
}: {
  id: string | null;
  onClose: () => void;
  onReload: () => void;
}) {
  const { data, error, isLoading } = useSWR<ApiListItem>(
    id ? `${REGISTRY}/apis/${id}` : null,
    (url: string) => api.get<ApiListItem>(url),
  );
  const {
    data: versionsResp,
    mutate: mutateVersions,
  } = useSWR<{ items: ApiVersion[] } | null>(
    id ? `/api/docs/v1/docs/apis/${id}/versions` : null,
    (url: string) => api.get<{ items: ApiVersion[] }>(url),
  );

  const [versionOpen, setVersionOpen] = useState(false);
  const versions = versionsResp?.items ?? [];

  async function versionAction(
    versionId: string,
    action: 'publish' | 'deprecate' | 'retire',
  ) {
    try {
      await api.post(`${REGISTRY}/api-versions/${versionId}/${action}`);
      message.success(`${action} 已执行`);
      await mutateVersions();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  const versionColumns: ColumnsType<ApiVersion> = [
    { title: '版本', dataIndex: 'version', width: 80 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (v) => <Tag color={STATUS_COLOR[v] ?? 'default'}>{v}</Tag>,
    },
    { title: 'backend_type', dataIndex: 'backend_type', width: 120 },
    {
      title: 'method / path',
      width: 220,
      render: (_, r) => (
        <Typography.Text code>
          {r.method} {r.path}
        </Typography.Text>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      render: (v) => dayjs(v as string).format('MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 100,
      render: (_, r) => {
        if (r.status === 'draft' || r.status === 'reviewing') {
          return (
            <Popconfirm
              title="发布该版本？将下发 APISIX 路由"
              onConfirm={() => void versionAction(r.id, 'publish')}
            >
              <Button size="small" type="link">
                发布
              </Button>
            </Popconfirm>
          );
        }
        if (r.status === 'published') {
          return (
            <Popconfirm
              title="标记废弃？仍可调用，给调用方迁移时间"
              onConfirm={() => void versionAction(r.id, 'deprecate')}
            >
              <Button size="small" type="link" danger>
                废弃
              </Button>
            </Popconfirm>
          );
        }
        if (r.status === 'deprecated') {
          return (
            <Popconfirm
              title="下线？调用将返 410 Gone"
              onConfirm={() => void versionAction(r.id, 'retire')}
            >
              <Button size="small" type="link" danger>
                下线
              </Button>
            </Popconfirm>
          );
        }
        return '—';
      },
    },
  ];

  return (
    <Drawer
      open={id !== null}
      onClose={onClose}
      width={920}
      title={data ? data.name : '加载中...'}
    >
      {isLoading && <div>加载中...</div>}
      {error && <div>加载失败：{(error as Error).message}</div>}
      {data && (
        <>
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
          </Descriptions>

          <div
            style={{
              marginTop: 24,
              marginBottom: 8,
              display: 'flex',
              justifyContent: 'space-between',
            }}
          >
            <strong>版本</strong>
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() => setVersionOpen(true)}
            >
              新建版本
            </Button>
          </div>
          <Table<ApiVersion>
            rowKey="id"
            size="small"
            columns={versionColumns}
            dataSource={versions}
            pagination={false}
            scroll={{ x: 800 }}
          />

          <ModalForm<ApiVersionCreateBody>
            title={`为 ${data.name} 新建版本`}
            width={560}
            open={versionOpen}
            onOpenChange={setVersionOpen}
            modalProps={{ destroyOnClose: true }}
            onFinish={async (values) => {
              try {
                await api.post(`${REGISTRY}/api-versions`, {
                  ...values,
                  api_id: data.id,
                });
                message.success('版本已创建（draft）');
                await mutateVersions();
                void onReload();
                return true;
              } catch (e) {
                message.error((e as Error).message);
                return false;
              }
            }}
          >
            <ProFormText
              name="version"
              label="版本号"
              required
              placeholder="v1, v2, ..."
              rules={[{ pattern: /^v\d+$/, message: '形如 v1 / v2' }]}
            />
            <ProFormSelect
              name="backend_type"
              label="backend_type"
              options={BACKEND_TYPE_OPTIONS}
              initialValue="http"
            />
            <ProFormText
              name="backend_url"
              label="backend_url"
              required
              placeholder="http://upstream-svc:port"
            />
            <ProFormSelect
              name="method"
              label="method"
              options={METHOD_OPTIONS}
              initialValue="GET"
            />
            <ProFormText
              name="path"
              label="path"
              required
              placeholder="/charge"
            />
            <ProFormText
              name="ai_model"
              label="ai_model（backend_type=ai_model 时）"
            />
            <ProFormSwitch name="ai_streaming" label="ai_streaming" />
          </ModalForm>
        </>
      )}
    </Drawer>
  );
}
