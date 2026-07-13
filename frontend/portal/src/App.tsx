import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Register } from './pages/Register';
import { Login } from './pages/Login';
import { Apps } from './pages/Apps';
import { ApiCatalog } from './pages/ApiCatalog';
import { ApiDetail } from './pages/ApiDetail';
import { useStore } from './store';

export default function App() {
  const auth = useStore((s) => s.auth);
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/register" element={<Register />} />
        <Route path="/login" element={<Login />} />
        <Route path="/apps" element={auth ? <Apps /> : <Navigate to="/login" />} />
        <Route path="/apis" element={auth ? <ApiCatalog /> : <Navigate to="/login" />} />
        <Route path="/apis/:id" element={auth ? <ApiDetail /> : <Navigate to="/login" />} />
        <Route path="*" element={<Navigate to={auth ? '/apis' : '/login'} />} />
      </Routes>
    </BrowserRouter>
  );
}
