import { useRef, useState } from 'react';
import {
  PageContainer,
  ProTable,
  type ActionType,
  type ProColumns,
} from '@ant-design/pro-components';
import {
  Button,
  Drawer,
  Tag,
  Typography,
  Space,
  message,
  Popconfirm,
  Descriptions,
  Modal,
  Input,
} from 'antd';
import {
  CheckOutlined,
  CloseOutlined,
  EyeOutlined,
  RollbackOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import useSWR from 'swr';

import { api, getAuth } from '../api/client';
import type {
  ChangeRequest,
  ChangeRequestStatus,
  TargetEnv,
} from '../api/types';

const STATUS_COLOR: Record<ChangeRequestStatus, string> = {
  pending: 'orange',
  approved: 'blue',
  rejected: 'red',
  applied: 'green',
  cancelled: 'default',
};

const ENV_COLOR: Record<TargetEnv, string> = {
  dev: 'default',
  staging: 'blue',
  prod: 'red',
};

type ReviewAction = 'approve' | 'reject';

export default function ChangeRequests() {
  const actionRef = useRef<ActionType | null>(null);
  const [drawerId, setDrawerId] = useState<number | null>(null);
  const [reviewTarget, setReviewTarget] = useState<
    { id: number; action: ReviewAction } | null
  >(null);
  const [reviewComment, setReviewComment] = useState('');

  const isPlatformAdmin = getAuth()?.user.isPlatformAdmin ?? false;

  const columns: ProColumns<ChangeRequest>[] = [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      search: false,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      valueType: 'select',
      valueEnum: {
        pending: { text: 'pending' },
        approved: { text: 'approved' },
        rejected: { text: 'rejected' },
        applied: { text: 'applied' },
        cancelled: { text: 'cancelled' },
      },
      render: (_, r) => <Tag color={STATUS_COLOR[r.status]}>{r.status}</Tag>,
    },
    {
      title: '变更类型',
      dataIndex: 'change_type',
      width: 110,
      valueType: 'select',
      valueEnum: {
        create: { text: 'create' },
        update: { text: 'update' },
        publish: { text: 'publish' },
        deprecate: { text: 'deprecate' },
        retire: { text: 'retire' },
      },
    },
    {
      title: '环境',
      dataIndex: 'target_env',
      width: 100,
      valueType: 'select',
      valueEnum: {
        dev: { text: 'dev' },
        staging: { text: 'staging' },
        prod: { text: 'prod' },
      },
      render: (_, r) => (
        <Tag color={ENV_COLOR[r.target_env]}>{r.target_env}</Tag>
      ),
    },
    {
      title: 'API ID',
      dataIndex: 'api_id',
      width: 90,
    },
    {
      title: '目标版本',
      dataIndex: 'target_version',
      width: 100,
      search: false,
    },
    {
      title: '提交人',
      dataIndex: 'submitted_by',
      width: 120,
    },
    {
      title: '提交时间',
      dataIndex: 'submitted_at',
      width: 160,
      search: false,
      render: (_, r) => dayjs(r.submitted_at).format('MM-DD HH:mm:ss'),
    },
    {
      title: 'diff 摘要',
      dataIndex: 'diff_summary',
      ellipsis: true,
      search: false,
      render: (v) => v || '—',
    },
    {
      title: '操作',
      width: 220,
      search: false,
      fixed: 'right',
      render: (_, r) => (
        <Space>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => setDrawerId(r.id)}
          >
            详情
          </Button>
          {r.status === 'pending' && isPlatformAdmin && (
            <>
              <Button
                size="small"
                type="link"
                icon={<CheckOutlined />}
                style={{ color: 'green' }}
                onClick={() => openReview(r.id, 'approve')}
              >
                批准
              </Button>
              <Button
                size="small"
                type="link"
                danger
                icon={<CloseOutlined />}
                onClick={() => openReview(r.id, 'reject')}
              >
                驳回
              </Button>
            </>
          )}
          {r.status === 'approved' && isPlatformAdmin && (
            <Popconfirm
              title="立即执行 apply？"
              onConfirm={() => applyRequest(r.id)}
            >
              <Button size="small" type="link" icon={<RollbackOutlined />}>
                Apply
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  function openReview(id: number, action: ReviewAction) {
    setReviewTarget({ id, action });
    setReviewComment('');
  }

  async function submitReview() {
    if (!reviewTarget) return;
    const { id, action } = reviewTarget;
    try {
      await api.post(`/api/registry/v1/change-requests/${id}/${action}`, {
        review_comment: reviewComment || null,
      });
      message.success(`已${action === 'approve' ? '批准' : '驳回'} #${id}`);
      setReviewTarget(null);
      actionRef.current?.reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  async function applyRequest(id: number) {
    try {
      const resp = await api.post<{ summary: string }>(
        `/api/registry/v1/change-requests/${id}/apply`,
      );
      message.success(`已 apply #${id}：${resp.summary}`);
      actionRef.current?.reload();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <PageContainer
      header={{
        title: '评审工单',
        extra: isPlatformAdmin ? (
          <Tag color="red">超管视角（可审批 prod）</Tag>
        ) : (
          <Tag>仅查看</Tag>
        ),
      }}
    >
      <ProTable<ChangeRequest>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        scroll={{ x: 1300 }}
        search={{ labelWidth: 'auto' }}
        request={async (params) => {
          try {
            const data = await api.get<ChangeRequest[]>(
              '/api/registry/v1/change-requests',
              {
                status: params.status,
                change_type: params.change_type,
                target_env: params.target_env,
                api_id: params.api_id,
                submitted_by: params.submitted_by,
                limit: params.pageSize || 20,
                offset:
                  ((params.current || 1) - 1) * (params.pageSize || 20),
              },
            );
            return {
              data,
              success: true,
              total:
                data.length < (params.pageSize || 20)
                  ? data.length
                  : -1,
            };
          } catch (e) {
            return {
              data: [],
              success: false,
              errorMessage: (e as Error).message,
            };
          }
        }}
        pagination={{ pageSize: 20 }}
      />

      <ChangeRequestDrawer
        id={drawerId}
        onClose={() => setDrawerId(null)}
      />

      <Modal
        open={reviewTarget !== null}
        title={
          reviewTarget?.action === 'approve'
            ? `批准 #${reviewTarget?.id}`
            : `驳回 #${reviewTarget?.id}`
        }
        onCancel={() => setReviewTarget(null)}
        onOk={submitReview}
        okText="提交"
        cancelText="取消"
        okButtonProps={{
          danger: reviewTarget?.action === 'reject',
        }}
      >
        <Input.TextArea
          rows={4}
          placeholder="审批意见（可选，最长 2000 字）"
          value={reviewComment}
          onChange={(e) => setReviewComment(e.target.value)}
          maxLength={2000}
          showCount
        />
      </Modal>
    </PageContainer>
  );
}

function ChangeRequestDrawer({
  id,
  onClose,
}: {
  id: number | null;
  onClose: () => void;
}) {
  const { data, error, isLoading } = useSWR<ChangeRequest>(
    id ? `/api/registry/v1/change-requests/${id}` : null,
    (url: string) => api.get<ChangeRequest>(url),
  );

  return (
    <Drawer
      open={id !== null}
      onClose={onClose}
      width={820}
      title={data ? `变更工单 #${data.id}` : '加载中'}
    >
      {isLoading && <div>加载中...</div>}
      {error && <div>加载失败：{(error as Error).message}</div>}
      {data && (
        <>
          <Descriptions column={2} size="small" bordered>
            <Descriptions.Item label="状态">
              <Tag color={STATUS_COLOR[data.status]}>{data.status}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="环境">
              <Tag color={ENV_COLOR[data.target_env]}>
                {data.target_env}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label="变更类型">
              {data.change_type}
            </Descriptions.Item>
            <Descriptions.Item label="目标版本">
              {data.target_version}
            </Descriptions.Item>
            <Descriptions.Item label="API ID">
              {data.api_id}
            </Descriptions.Item>
            <Descriptions.Item label="提交人">
              {data.submitted_by}
            </Descriptions.Item>
            <Descriptions.Item label="提交时间" span={2}>
              {dayjs(data.submitted_at).format('YYYY-MM-DD HH:mm:ss')}
            </Descriptions.Item>
            {data.dingtalk_approval_id && (
              <Descriptions.Item label="钉钉审批单" span={2}>
                <Typography.Text code>
                  {data.dingtalk_approval_id}
                </Typography.Text>
              </Descriptions.Item>
            )}
            {data.diff_summary && (
              <Descriptions.Item label="diff 摘要" span={2}>
                <Typography.Text>{data.diff_summary}</Typography.Text>
              </Descriptions.Item>
            )}
            {data.reviewed_by && (
              <Descriptions.Item label="审核人">
                {data.reviewed_by}
              </Descriptions.Item>
            )}
            {data.reviewed_at && (
              <Descriptions.Item label="审核时间">
                {dayjs(data.reviewed_at).format('MM-DD HH:mm:ss')}
              </Descriptions.Item>
            )}
            {data.review_comment && (
              <Descriptions.Item label="审核意见" span={2}>
                <Typography.Text
                  type={data.status === 'rejected' ? 'danger' : undefined}
                >
                  {data.review_comment}
                </Typography.Text>
              </Descriptions.Item>
            )}
            {data.applied_at && (
              <Descriptions.Item label="Apply 时间" span={2}>
                {dayjs(data.applied_at).format('YYYY-MM-DD HH:mm:ss')}
              </Descriptions.Item>
            )}
          </Descriptions>

          <Typography.Title level={5} style={{ marginTop: 24 }}>
            proposed_config
          </Typography.Title>
          <pre
            style={{
              background: '#f6f8fa',
              padding: 12,
              borderRadius: 6,
              maxHeight: 300,
              overflow: 'auto',
              fontSize: 12,
            }}
          >
            {JSON.stringify(data.proposed_config, null, 2)}
          </pre>

          {data.current_config && (
            <>
              <Typography.Title level={5} style={{ marginTop: 16 }}>
                current_config
              </Typography.Title>
              <pre
                style={{
                  background: '#f6f8fa',
                  padding: 12,
                  borderRadius: 6,
                  maxHeight: 300,
                  overflow: 'auto',
                  fontSize: 12,
                }}
              >
                {JSON.stringify(data.current_config, null, 2)}
              </pre>
            </>
          )}
        </>
      )}
    </Drawer>
  );
}
