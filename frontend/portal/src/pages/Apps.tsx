import { useRef, useState } from 'react';
import {
  ModalForm,
  PageContainer,
  ProFormText,
  ProTable,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import { Alert, Button, Modal, Tag, Typography, message } from 'antd';
import { KeyOutlined, PlusOutlined } from '@ant-design/icons';

import { api } from '../api/client';

interface App {
  id: string;
  name: string;
  tenant_id: string;
  status: string;
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
