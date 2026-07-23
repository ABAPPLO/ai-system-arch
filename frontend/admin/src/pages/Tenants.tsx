import { useRef, useState } from 'react';
import {
  ModalForm,
  PageContainer,
  ProFormSelect,
  ProFormText,
  ProTable,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import {
  Button,
  Descriptions,
  Drawer,
  InputNumber,
  message,
  Popconfirm,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
} from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import dayjs from 'dayjs';
import useSWR from 'swr';

import { api, getAuth } from '../api/client';
import type {
  TenantCreateBody,
  TenantListItem,
  TenantMember,
  TenantQuota,
  TenantUpdateBody,
  TenantUsage,
} from '../api/types';

const STATUS_COLOR: Record<string, string> = {
  active: 'green',
  suspended: 'orange',
  closed: 'red',
};

const TYPE_OPTIONS = [
  { label: 'internal', value: 'internal' },
  { label: 'external', value: 'external' },
  { label: 'system', value: 'system' },
];
const TIER_OPTIONS = [
  { label: 'free', value: 'free' },
  { label: 'standard', value: 'standard' },
  { label: 'premium', value: 'premium' },
];
const ROLE_OPTIONS = [
  { label: 'owner', value: 'owner' },
  { label: 'admin', value: 'admin' },
  { label: 'developer', value: 'developer' },
  { label: 'viewer', value: 'viewer' },
];

const BASE = '/api/tenant/v1/tenant/tenants';

export default function Tenants() {
  const actionRef = useRef<ActionType | null>(null);
  const [drawerId, setDrawerId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<TenantListItem | null>(null);

  const isPlatformAdmin = getAuth()?.user.isPlatformAdmin ?? false;

  const columns: ProColumns<TenantListItem>[] = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 170,
      ellipsis: true,
      copyable: true,
      search: false,
    },
    { title: '名称', dataIndex: 'name', width: 150, search: false },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      valueType: 'select',
      valueEnum: {
        active: { text: 'active' },
        suspended: { text: 'suspended' },
        closed: { text: 'closed' },
      },
      render: (_, r) => <Tag color={STATUS_COLOR[r.status]}>{r.status}</Tag>,
    },
    {
      title: '类型',
      dataIndex: 'type',
      width: 100,
      valueType: 'select',
      valueEnum: {
        internal: { text: 'internal' },
        external: { text: 'external' },
        system: { text: 'system' },
      },
    },
    { title: 'tier', dataIndex: 'tier', width: 90, search: false },
    {
      title: '父租户',
      dataIndex: 'parent_id',
      width: 150,
      search: false,
      render: (v) => (v as string) || '—',
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      search: false,
      render: (v) => dayjs(v as string).format('MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 160,
      fixed: 'right',
      search: false,
      render: (_, r) => (
        <Space>
          <Button size="small" onClick={() => setDrawerId(r.id)}>
            详情
          </Button>
          {isPlatformAdmin && r.status !== 'closed' && (
            <Button size="small" type="link" onClick={() => setEditTarget(r)}>
              编辑
            </Button>
          )}
        </Space>
      ),
    },
  ];

  async function lifecycle(
    id: string,
    action: 'suspend' | 'resume' | 'close',
  ): Promise<void> {
    try {
      await api.post<TenantListItem>(`${BASE}/${id}/${action}`);
      message.success(`${action} 已执行`);
      actionRef.current?.reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <PageContainer header={{ title: '租户管理' }}>
      <ProTable<TenantListItem>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 1100 }}
        search={{ labelWidth: 'auto' }}
        toolBarRender={() =>
          isPlatformAdmin
            ? [
                <Button
                  key="create"
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setCreateOpen(true)}
                >
                  新建租户
                </Button>,
              ]
            : []
        }
        request={async (params) => {
          try {
            const data = await api.get<TenantListItem[]>(BASE, {
              status: params.status,
              type: params.type,
              limit: params.pageSize || 100,
              offset:
                ((params.current || 1) - 1) * (params.pageSize || 100),
            });
            return {
              data,
              success: true,
              total:
                data.length < (params.pageSize || 100) ? data.length : -1,
            };
          } catch (e) {
            return {
              data: [],
              success: false,
              errorMessage: (e as Error).message,
            };
          }
        }}
        pagination={{ pageSize: 100 }}
      />

      <TenantDrawer
        id={drawerId}
        onClose={() => setDrawerId(null)}
        onLifecycle={lifecycle}
      />

      {/* 新建 */}
      <ModalForm<TenantCreateBody>
        title="新建租户"
        width={520}
        open={createOpen}
        onOpenChange={setCreateOpen}
        modalProps={{ destroyOnClose: true }}
        onFinish={async (values) => {
          try {
            await api.post(BASE, values);
            message.success('租户已创建');
            actionRef.current?.reload();
            return true;
          } catch (e) {
            message.error((e as Error).message);
            return false;
          }
        }}
      >
        <ProFormText
          name="id"
          label="租户 ID"
          required
          tooltip="唯一标识，创建后不可改"
          rules={[
            {
              pattern: /^[a-zA-Z0-9_-]+$/,
              message: '仅字母/数字/下划线/连字符',
            },
          ]}
        />
        <ProFormText name="name" label="名称" required />
        <ProFormText
          name="slug"
          label="slug"
          required
          rules={[
            { pattern: /^[a-z0-9-]+$/, message: '小写字母/数字/连字符' },
          ]}
        />
        <ProFormSelect
          name="type"
          label="类型"
          options={TYPE_OPTIONS}
          initialValue="internal"
        />
        <ProFormSelect
          name="tier"
          label="tier"
          options={TIER_OPTIONS}
          initialValue="standard"
        />
        <ProFormText name="parent_id" label="父租户 ID（可选）" />
      </ModalForm>

      {/* 编辑 */}
      <ModalForm<TenantUpdateBody>
        title={`编辑租户 ${editTarget?.id ?? ''}`}
        width={520}
        open={editTarget !== null}
        onOpenChange={(open) => !open && setEditTarget(null)}
        modalProps={{ destroyOnClose: true }}
        initialValues={editTarget ?? undefined}
        onFinish={async (values) => {
          if (!editTarget) return false;
          try {
            await api.put<TenantListItem>(`${BASE}/${editTarget.id}`, values);
            message.success('已保存');
            actionRef.current?.reload();
            return true;
          } catch (e) {
            message.error((e as Error).message);
            return false;
          }
        }}
      >
        <ProFormText name="name" label="名称" required />
        <ProFormText
          name="slug"
          label="slug"
          rules={[
            { pattern: /^[a-z0-9-]+$/, message: '小写字母/数字/连字符' },
          ]}
        />
        <ProFormSelect name="tier" label="tier" options={TIER_OPTIONS} />
      </ModalForm>
    </PageContainer>
  );
}

function TenantDrawer({
  id,
  onClose,
  onLifecycle,
}: {
  id: string | null;
  onClose: () => void;
  onLifecycle: (
    id: string,
    action: 'suspend' | 'resume' | 'close',
  ) => Promise<void>;
}) {
  const isPlatformAdmin = getAuth()?.user.isPlatformAdmin ?? false;

  const { data, error, isLoading, mutate } = useSWR<TenantListItem>(
    id ? `${BASE}/${id}` : null,
    (u: string) => api.get<TenantListItem>(u),
  );
  const { data: members, mutate: mutateMembers } = useSWR<TenantMember[]>(
    id ? `${BASE}/${id}/members` : null,
    (u: string) => api.get<TenantMember[]>(u),
  );
  const { data: quota, mutate: mutateQuota } = useSWR<TenantQuota>(
    id ? `${BASE}/${id}/quota` : null,
    (u: string) => api.get<TenantQuota>(u),
  );
  const { data: usage } = useSWR<TenantUsage>(
    id ? `${BASE}/${id}/usage` : null,
    (u: string) => api.get<TenantUsage>(u),
  );

  const [dayLimit, setDayLimit] = useState<number | null>(null);
  const [memberOpen, setMemberOpen] = useState(false);

  async function changeRole(userId: string, role: string) {
    if (!id) return;
    try {
      await api.put(`${BASE}/${id}/members/${userId}`, { role });
      message.success('角色已更新');
      await mutateMembers();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  async function removeMember(userId: string) {
    if (!id) return;
    try {
      await api.del(`${BASE}/${id}/members/${userId}`);
      message.success('已移除');
      await mutateMembers();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  async function saveQuota() {
    if (!id || dayLimit === null) return;
    try {
      await api.put<TenantQuota>(`${BASE}/${id}/quota`, {
        day_limit: dayLimit,
        rate_limit: quota?.rate_limit ?? {},
      });
      message.success('配额已保存');
      await mutateQuota();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  async function doLifecycle(
    tenantId: string,
    action: 'suspend' | 'resume' | 'close',
  ) {
    await onLifecycle(tenantId, action);
    await mutate();
  }

  const memberColumns: ColumnsType<TenantMember> = [
    { title: 'user_id', dataIndex: 'user_id' },
    {
      title: '角色',
      dataIndex: 'role',
      width: 150,
      render: (_, r) =>
        isPlatformAdmin ? (
          <Select
            size="small"
            value={r.role}
            options={ROLE_OPTIONS}
            style={{ width: 120 }}
            onChange={(v) => void changeRole(r.user_id, v)}
          />
        ) : (
          r.role
        ),
    },
    {
      title: '加入时间',
      dataIndex: 'created_at',
      width: 150,
      render: (v) => dayjs(v as string).format('MM-DD HH:mm'),
    },
    {
      title: '操作',
      width: 80,
      render: (_, r) => (
        <Popconfirm
          title={`移除成员 ${r.user_id}？`}
          onConfirm={() => void removeMember(r.user_id)}
        >
          <Button size="small" type="link" danger>
            移除
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <Drawer
      open={id !== null}
      onClose={onClose}
      width={820}
      title={data ? `${data.name} (${data.id})` : '加载中...'}
      extra={
        data && isPlatformAdmin && data.status !== 'closed' ? (
          <Space>
            {data.status === 'active' && (
              <Popconfirm
                title="暂停该租户？所有调用将被拒绝"
                onConfirm={() => void doLifecycle(data.id, 'suspend')}
              >
                <Button size="small" danger>
                  暂停
                </Button>
              </Popconfirm>
            )}
            {data.status === 'suspended' && (
              <Popconfirm
                title="恢复该租户？"
                onConfirm={() => void doLifecycle(data.id, 'resume')}
              >
                <Button size="small">恢复</Button>
              </Popconfirm>
            )}
            <Popconfirm
              title="关闭该租户？Key 将失效，数据保留归档"
              onConfirm={() => void doLifecycle(data.id, 'close')}
            >
              <Button size="small" danger>
                关闭
              </Button>
            </Popconfirm>
          </Space>
        ) : null
      }
    >
      {isLoading && <div>加载中...</div>}
      {error && <div>加载失败：{(error as Error).message}</div>}
      {data && (
        <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="状态">
              <Tag color={STATUS_COLOR[data.status]}>{data.status}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="类型">{data.type}</Descriptions.Item>
            <Descriptions.Item label="tier">{data.tier}</Descriptions.Item>
            <Descriptions.Item label="slug">{data.slug}</Descriptions.Item>
            <Descriptions.Item label="父租户">
              {data.parent_id || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="创建时间">
              {dayjs(data.created_at).format('YYYY-MM-DD HH:mm:ss')}
            </Descriptions.Item>
          </Descriptions>

          <div style={{ marginTop: 16, marginBottom: 8 }}>
            <Space size={48}>
              <Statistic
                title="今日用量"
                value={usage?.day_used ?? '—'}
                suffix={usage ? `/ ${usage.day_limit || '∞'}` : ''}
              />
              <Statistic title="剩余" value={usage?.remaining ?? '—'} />
            </Space>
          </div>

          {/* 配额 */}
          <div style={{ marginTop: 16 }}>
            <Space>
              <span>日配额上限（day_limit，0=不限）：</span>
              <InputNumber
                size="small"
                min={0}
                value={dayLimit ?? quota?.day_limit ?? 0}
                onChange={(v) => setDayLimit(typeof v === 'number' ? v : 0)}
                disabled={!isPlatformAdmin}
              />
              {isPlatformAdmin && (
                <Button size="small" type="primary" onClick={saveQuota}>
                  保存配额
                </Button>
              )}
            </Space>
          </div>

          {/* 成员 */}
          <div
            style={{
              marginTop: 24,
              marginBottom: 8,
              display: 'flex',
              justifyContent: 'space-between',
            }}
          >
            <strong>成员</strong>
            {isPlatformAdmin && (
              <Button
                size="small"
                icon={<PlusOutlined />}
                onClick={() => setMemberOpen(true)}
              >
                添加成员
              </Button>
            )}
          </div>
          <Table<TenantMember>
            rowKey="id"
            size="small"
            columns={memberColumns}
            dataSource={members}
            pagination={false}
            loading={!members && !error}
          />

          <ModalForm<{ user_id: string; role: string }>
            title="添加成员"
            width={440}
            open={memberOpen}
            onOpenChange={setMemberOpen}
            modalProps={{ destroyOnClose: true }}
            onFinish={async (values) => {
              if (!id) return false;
              try {
                await api.post(`${BASE}/${id}/members`, values);
                message.success('已添加');
                await mutateMembers();
                return true;
              } catch (e) {
                message.error((e as Error).message);
                return false;
              }
            }}
          >
            <ProFormText name="user_id" label="user_id" required />
            <ProFormSelect
              name="role"
              label="角色"
              options={ROLE_OPTIONS}
              initialValue="developer"
            />
          </ModalForm>
        </>
      )}
    </Drawer>
  );
}
