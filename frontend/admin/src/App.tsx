import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Spin } from 'antd';

import { isJwtExpired } from '@apihub/shared';
import { getAuth } from './api/client';
import Layout from './components/Layout';
import Login from './pages/Login';
import LoginCallback from './pages/LoginCallback';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const Retry = lazy(() => import('./pages/Retry'));
const ChangeRequests = lazy(() => import('./pages/ChangeRequests'));
const Apis = lazy(() => import('./pages/Apis'));
const AuditLog = lazy(() => import('./pages/AuditLog'));
const Calls = lazy(() => import('./pages/Calls'));
const Tenants = lazy(() => import('./pages/Tenants'));
const Billing = lazy(() => import('./pages/Billing').then(m => ({ default: m.Billing })));

function RequireAuth({ children }: { children: React.ReactNode }) {
  const auth = getAuth();
  // token 缺失或本地判定已过期都直接回登录（此前只查存在，过期用户会先闪一屏再被 401 弹回）
  if (!auth || isJwtExpired(auth.token)) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function PageFallback() {
  return (
    <div style={{ padding: 64, textAlign: 'center' }}>
      <Spin size="large" />
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/login/callback" element={<LoginCallback />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route
          index
          element={
            <Suspense fallback={<PageFallback />}>
              <Dashboard />
            </Suspense>
          }
        />
        <Route
          path="retry"
          element={
            <Suspense fallback={<PageFallback />}>
              <Retry />
            </Suspense>
          }
        />
        <Route
          path="change-requests"
          element={
            <Suspense fallback={<PageFallback />}>
              <ChangeRequests />
            </Suspense>
          }
        />
        <Route
          path="apis"
          element={
            <Suspense fallback={<PageFallback />}>
              <Apis />
            </Suspense>
          }
        />
        <Route
          path="audit"
          element={
            <Suspense fallback={<PageFallback />}>
              <AuditLog />
            </Suspense>
          }
        />
        <Route
          path="calls"
          element={
            <Suspense fallback={<PageFallback />}>
              <Calls />
            </Suspense>
          }
        />
        <Route
          path="tenants"
          element={
            <Suspense fallback={<PageFallback />}>
              <Tenants />
            </Suspense>
          }
        />
        <Route
          path="billing"
          element={
            <Suspense fallback={<PageFallback />}>
              <Billing />
            </Suspense>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
