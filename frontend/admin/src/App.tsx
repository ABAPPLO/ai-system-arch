import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Spin } from 'antd';

import { getAuth } from './api/client';
import Layout from './components/Layout';
import Login from './pages/Login';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const Retry = lazy(() => import('./pages/Retry'));
const ChangeRequests = lazy(() => import('./pages/ChangeRequests'));

function RequireAuth({ children }: { children: React.ReactNode }) {
  const auth = getAuth();
  if (!auth) return <Navigate to="/login" replace />;
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
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
