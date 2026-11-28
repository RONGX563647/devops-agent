/** 网关清单与模型列表的取数逻辑。
 *
 * 从抽屉里抽出来的两个理由：
 * 1. 抽屉已经 500 行上下，再塞两段 async 取数就到读不动的地步（项目有单文件上限）；
 * 2. 这两个请求的**时机**是有约定的 —— 打开抽屉只读本地注册表，聚焦模型那一行
 *    才可能打上游，点刷新才强制绕过缓存。约定藏在一个地方比散在渲染里好维持。
 */

import { useState } from 'react';
import { fetchProviderModels, fetchProviders, refreshProviderModels } from '../../../api/client';
import type { ProviderModel, ProvidersPayload } from '../../../types';

/** 一次模型列表取数的结果。`code` 为 `''` 表示还没取过。 */
export interface ModelsState {
  loading: boolean;
  /** 后端发现状态码：ok / unauthorized / unsupported / …（失败也是 200）。 */
  code: string;
  total: number;
  /** 回的是旧缓存：上游此刻不可达，但列表还能用。 */
  stale: boolean;
}

const IDLE: ModelsState = { loading: false, code: '', total: 0, stale: false };

export interface CatalogState {
  providers: ProvidersPayload | null;
  /** 注册表读不出来时的提示（`/api/providers` 回 500）。 */
  error: string;
  models: ProviderModel[];
  state: ModelsState;
  loadProviders: () => Promise<void>;
  loadModels: (providerId: string, force: boolean) => Promise<void>;
  /** 换网关时清空上一家的候选：否则会拿 A 的列表去查 B。 */
  resetModels: () => void;
  /** 打开抽屉：回到「什么都没取过」的干净状态。 */
  reset: () => void;
}

export function useCatalog(): CatalogState {
  const [providers, setProviders] = useState<ProvidersPayload | null>(null);
  const [error, setError] = useState('');
  const [models, setModels] = useState<ProviderModel[]>([]);
  const [state, setState] = useState<ModelsState>(IDLE);

  async function loadProviders(): Promise<void> {
    setError('');
    try {
      setProviders(await fetchProviders());
    } catch {
      // 500 providers_invalid：注册表有语法或取值错误。空列表会被当成「没配网关」，
      // 所以这里必须留下提示，而不是安静地画一个空下拉。
      setProviders(null);
      setError('读取网关清单失败：config/providers.yaml 可能有语法或取值错误');
    }
  }

  async function loadModels(providerId: string, force: boolean): Promise<void> {
    if (!providerId) return;
    setState((prev) => ({ ...prev, loading: true }));
    try {
      const body = force
        ? await refreshProviderModels(providerId)
        : await fetchProviderModels(providerId);
      setModels(body.items);
      setState({ loading: false, code: body.code, total: body.total, stale: body.stale });
    } catch {
      // 走到这里是 4xx/5xx（例如网关 id 不在注册表里）。发现失败本身是 200 + code，
      // 所以「取不到列表」在这里只剩这一种原因，文案不必再细分。
      setModels([]);
      setState({ loading: false, code: 'unavailable', total: 0, stale: false });
    }
  }

  function resetModels(): void {
    setModels([]);
    setState(IDLE);
  }

  function reset(): void {
    setProviders(null);
    setError('');
    resetModels();
  }

  return { providers, error, models, state, loadProviders, loadModels, resetModels, reset };
}
