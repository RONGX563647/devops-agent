import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { Cost } from './pages/Cost';
import { Dashboard } from './pages/Dashboard';
import { Quality } from './pages/Quality';
import { RunDetail } from './pages/RunDetail';
import { Runs } from './pages/Runs';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="app-shell">
          <header className="app-header">
            <div className="brand">DevOps Agent · 可观测性</div>
            <nav>
              <Link to="/">总览</Link>
              <Link to="/runs">运行列表</Link>
              <Link to="/quality">质量评估</Link>
              <Link to="/cost">成本性能</Link>
            </nav>
          </header>
          <main className="app-main">
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/runs" element={<Runs />} />
              <Route path="/runs/:id" element={<RunDetail />} />
              <Route path="/quality" element={<Quality />} />
              <Route path="/cost" element={<Cost />} />
            </Routes>
          </main>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
