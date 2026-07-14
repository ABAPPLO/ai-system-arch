import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Register } from './pages/Register';
import { Login } from './pages/Login';
import { Apps } from './pages/Apps';
import { ApiCatalog } from './pages/ApiCatalog';
import { ApiDetail } from './pages/ApiDetail';
import { Usage } from './pages/Usage';
import { Webhooks } from './pages/Webhooks';
import { Plans } from './pages/Plans';
import { Invoices } from './pages/Invoices';
import { Privacy } from './pages/Privacy';
import { Analytics } from './pages/Analytics';
import { Layout } from './Layout';
import { useStore } from './store';

export default function App() {
  const auth = useStore((s) => s.auth);
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="/login" element={<Login />} />
        <Route path="/apps" element={auth ? <Layout><Apps /></Layout> : <Navigate to="/login" />} />
        <Route path="/apis" element={auth ? <Layout><ApiCatalog /></Layout> : <Navigate to="/login" />} />
        <Route path="/apis/:id" element={auth ? <Layout><ApiDetail /></Layout> : <Navigate to="/login" />} />
        <Route path="/usage" element={auth ? <Layout><Usage /></Layout> : <Navigate to="/login" />} />
        <Route path="/webhooks" element={auth ? <Layout><Webhooks /></Layout> : <Navigate to="/login" />} />
        <Route path="/plans" element={auth ? <Layout><Plans /></Layout> : <Navigate to="/login" />} />
        <Route path="/invoices" element={auth ? <Layout><Invoices /></Layout> : <Navigate to="/login" />} />
        <Route path="/privacy" element={auth ? <Layout><Privacy /></Layout> : <Navigate to="/login" />} />
        <Route path="/analytics" element={auth ? <Layout><Analytics /></Layout> : <Navigate to="/login" />} />
        <Route path="*" element={<Navigate to={auth ? '/apis' : '/login'} />} />
      </Routes>
    </BrowserRouter>
  );
}
