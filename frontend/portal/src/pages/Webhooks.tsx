import { useRef, useState } from 'react';
import {
  ModalForm,
  PageContainer,
  ProFormText,
  ProTable,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import { Button, Popconfirm, Space, Tag, Typography, message } from 'antd';
import { PlusOutlined, ThunderboltOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';

import { api } from '../api/client';

interface Webhook {
  id: string;
  url: string;
  events: string[];
  status: string;
  created_at: string;
}

const STATUS_COLOR: Record<string, string> = {
  active: 'green',
  disabled: 'default',
};

export function Webhooks() {
  const actionRef = useRef<ActionType | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  async function test(id: string) {
    try {
      const r = await api.post<{
        success: boolean;
        status_code: number | null;
        latency_ms: number | null;
        error: string | null;
      }>(`/v1/portal/webhooks/${id}/test`);
      if (r.success) {
        message.success(`✅ ${r.status_code} in ${r.latency_ms}ms`);
      } else {
        message.error(`❌ ${r.error || r.status_code}`);
      }
    } catch (e) {
      message.error(`❌ ${String(e)}`);
    }
  }

  async function remove(id: string) {
    try {
      await api.del(`/v1/portal/webhooks/${id}`);
      message.success('已删除');
      actionRef.current?.reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  const columns: ProColumns<Webhook>[] = [
    {
      title: 'URL',
      dataIndex: 'url',
      ellipsis: true,
      render: (_, r) => <Typography.Text code>{r.url}</Typography.Text>,
    },
    {
      title: '事件',
      dataIndex: 'events',
      width: 280,
      search: false,
      render: (_, r) =>
        r.events && r.events.length ? (
          <Space size={[4, 4]} wrap>
            {r.events.map((e) => (
              <Tag key={e}>{e}</Tag>
            ))}
          </Space>
        ) : (
          '—'
        ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      search: false,
      render: (_, r) => (
        <Tag color={STATUS_COLOR[r.status] ?? 'default'}>{r.status}</Tag>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      search: false,
      render: (v) => dayjs(v as string).format('YYYY-MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 170,
      fixed: 'right',
      search: false,
      render: (_, r) => (
        <Space>
          <Button
            size="small"
            icon={<ThunderboltOutlined />}
            onClick={() => void test(r.id)}
          >
            测试
          </Button>
          <Popconfirm
            title="确认删除该 Webhook？"
            onConfirm={() => void remove(r.id)}
          >
            <Button size="small" type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <PageContainer header={{ title: 'Webhook 通知' }}>
      <ProTable<Webhook>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        search={false}
        pagination={false}
        scroll={{ x: 1000 }}
        toolBarRender={() => [
          <Button
            key="create"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreateOpen(true)}
          >
            新增 Webhook
          </Button>,
        ]}
        request={async () => {
          try {
            const data = await api.get<Webhook[]>('/v1/portal/webhooks');
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

      <ModalForm<{ url: string; events: string }>
        title="新增 Webhook"
        width={520}
        open={createOpen}
        onOpenChange={setCreateOpen}
        modalProps={{ destroyOnClose: true }}
        onFinish={async (values) => {
          const events = values.events
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean);
          try {
            await api.post('/v1/portal/webhooks', {
              url: values.url,
              events,
            });
            message.success('已创建');
            actionRef.current?.reload();
            return true;
          } catch (e) {
            message.error((e as Error).message);
            return false;
          }
        }}
      >
        <ProFormText
          name="url"
          label="回调 URL"
          required
          placeholder="https://example.com/webhook"
          rules={[{ type: 'url', message: '请输入合法 URL' }]}
        />
        <ProFormText
          name="events"
          label="事件类型"
          required
          placeholder="api.call.succeeded,api.call.failed"
          tooltip="逗号分隔"
        />
      </ModalForm>
    </PageContainer>
  );
}
