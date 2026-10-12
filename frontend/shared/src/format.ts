/**
 * 双前端共享格式化工具（此前 fmtCents 在 admin/Billing 与 portal/Invoices 各一份）。
 */

/** 分（cents）→ 人民币展示，如 99900 → ¥999.00 */
export function fmtCents(c: number): string {
  return `¥${(c / 100).toFixed(2)}`;
}
