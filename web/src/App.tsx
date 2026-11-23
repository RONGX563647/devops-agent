import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Cost } from './pages/Cost';
import { Dashboard } from './pages/Dashboard';
import { Quality } from './pages/Quality';
import { RunDetail } from './pages/RunDetail';
import { Runs } from './pages/Runs';
import { Workspace } from './workspace/Workspace';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Workspace />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/quality" element={<Quality />} />
          <Route path="/cost" element={<Cost />} />
          <Route path="/dashboard" element={<Dashboard />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
