import { useEffect, useRef, useState } from 'react';
import {
  ModalForm,
  PageContainer,
  ProFormText,
  ProTable,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import { Alert, Button, Modal, Popconfirm, Space, Table, Tag, Typography, message } from 'antd';
import { KeyOutlined, PlusOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import type { ColumnsType } from 'antd/es/table';

import { api } from '../api/client';

interface App {
  id: string;
  name: string;
  tenant_id: string;
  status: string;
}

interface AppKey {
  id: string;
  name: string;
  display_prefix: string;
  status: string;
  signing: boolean;
  created_at: string;
  last_used_at: string | null;
}

const STATUS_COLOR: Record<string, string> = {
  active: 'green',
  disabled: 'default',
  suspended: 'orange',
};

export function Apps() {
  const actionRef = useRef<ActionType | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [newKey, setNewKey] = useState<string | null>(null);

  const genKey = async (appId: string) => {
    try {
      const r = await api.post<{ api_key: string }>(
        `/v1/portal/apps/${appId}/api-keys`,
        { name: 'default' },
      );
      setNewKey(r.api_key);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const columns: ProColumns<App>[] = [
    { title: '名称', dataIndex: 'name', width: 180 },
    {
      title: 'ID',
      dataIndex: 'id',
      ellipsis: true,
      copyable: true,
    },
    { title: '租户', dataIndex: 'tenant_id', ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (_, r) => (
        <Tag color={STATUS_COLOR[r.status] ?? 'default'}>{r.status}</Tag>
      ),
    },
    {
      title: '操作',
      width: 140,
      fixed: 'right',
      search: false,
      render: (_, r) => (
        <Button
          size="small"
          icon={<KeyOutlined />}
          onClick={() => void genKey(r.id)}
        >
          生成 Key
        </Button>
      ),
    },
  ];

  return (
    <PageContainer header={{ title: '我的应用' }}>
      <ProTable<App>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        search={false}
        pagination={false}
        scroll={{ x: 900 }}
        expandable={{
          expandedRowRender: (r) => <AppKeys appId={r.id} />,
          rowExpandable: () => true,
        }}
        toolBarRender={() => [
          <Button
            key="create"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            新建应用
          </Button>,
        ]}
        request={async () => {
          try {
            const data = await api.get<App[]>('/v1/portal/apps', undefined);
            return { data, success: true };
          } catch (e) {
            return {
              data: [],
              success: false,
              errorMessage: (e as Error).message,
            };
          }
        }}
      />

      <ModalForm<{ name: string }>
        title="新建应用"
        width={480}
        open={createOpen}
        onOpenChange={setCreateOpen}
        modalProps={{ destroyOnClose: true }}
        onFinish={async (values) => {
          try {
            await api.post('/v1/portal/apps', {
              name: values.name,
              type: 'external',
            });
            message.success('应用已创建');
            actionRef.current?.reload();
            return true;
          } catch (e) {
            message.error((e as Error).message);
            return false;
          }
        }}
      >
        <ProFormText
          name="name"
          label="应用名"
          required
          placeholder="如 my-app"
        />
      </ModalForm>

      <Modal
        open={newKey !== null}
        title="API Key 已生成"
        onOk={() => setNewKey(null)}
        onCancel={() => setNewKey(null)}
        okText="我已复制保存"
        cancelText="关闭"
      >
        <Alert
          type="warning"
          showIcon
          message="Key 仅显示一次，请立即复制保存"
          description={
            <Typography.Text
              code
              copyable
              style={{ wordBreak: 'break-all', display: 'block' }}
            >
              {newKey ?? ''}
            </Typography.Text>
          }
        />
      </Modal>
    </PageContainer>
  );
}

function AppKeys({ appId }: { appId: string }) {
  const [keys, setKeys] = useState<AppKey[]>([]);
  const [loading, setLoading] = useState(false);
  const [rotated, setRotated] = useState<string | null>(null);

  const reload = async () => {
    setLoading(true);
    try {
      const r = await api.get<AppKey[]>(`/v1/portal/apps/${appId}/api-keys`);
      setKeys(r);
    } catch (e) {
      message.error((e as Error).message);
      setKeys([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
  }, [appId]);

  const revoke = async (keyId: string) => {
    try {
      await api.del(`/v1/portal/api-keys/${keyId}`);
      message.success('已吊销');
      void reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const rotate = async (keyId: string) => {
    try {
      const r = await api.post<{ hmac_secret: string }>(
        `/v1/portal/api-keys/${keyId}/hmac-secret/rotate`,
      );
      setRotated(r.hmac_secret);
    } catch (e) {
      message.error((e as Error).message);
    }
  };

  const columns: ColumnsType<AppKey> = [
    { title: '前缀', dataIndex: 'display_prefix', render: (v) => <Typography.Text code>{v}…</Typography.Text> },
    { title: '名称', dataIndex: 'name' },
    { title: '状态', dataIndex: 'status', width: 100, render: (v) => <Tag color={v === 'active' ? 'green' : 'default'}>{v}</Tag> },
    { title: '创建', dataIndex: 'created_at', width: 150, render: (v) => dayjs(v).format('MM-DD HH:mm') },
    { title: '最后使用', dataIndex: 'last_used_at', width: 150, render: (v) => (v ? dayjs(v).format('MM-DD HH:mm') : '—') },
    {
      title: '操作',
      width: 160,
      render: (_, r) => (
        <Space>
          {r.status === 'active' && (
            <Popconfirm title="吊销该 Key？不可恢复。" okText="吊销" okButtonProps={{ danger: true }} onConfirm={() => void revoke(r.id)}>
              <Button size="small" danger>吊销</Button>
            </Popconfirm>
          )}
          {r.signing && r.status === 'active' && (
            <Popconfirm title="轮换 HMAC secret？旧 secret 立即失效。" okText="轮换" onConfirm={() => void rotate(r.id)}>
              <Button size="small">轮换</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <>
      <Table<AppKey> rowKey="id" size="small" columns={columns} dataSource={keys} loading={loading} pagination={false} />
      <Modal
        open={rotated !== null}
        title="新 HMAC Secret（仅显示一次）"
        okText="我已复制保存"
        cancelText="关闭"
        onOk={() => setRotated(null)}
        onCancel={() => setRotated(null)}
      >
        <Alert
          type="warning"
          showIcon
          message="请立即复制保存，旧 secret 已失效"
          description={
            <Typography.Text code copyable style={{ wordBreak: 'break-all', display: 'block' }}>
              {rotated ?? ''}
            </Typography.Text>
          }
        />
      </Modal>
    </>
  );
}
