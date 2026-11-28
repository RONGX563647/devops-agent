/**
 * 设置页外壳（SPEC-0022 §4）：两段左树 + 右侧内容区。
 *
 * 为什么不再用一个组件渲染所有域：旧实现 385 行里混了「域清单 / 域编辑 / 行渲染」
 * 三件事，加一个二级导航就会突破单文件上限。现在拆成
 * `SectionTree`（导航）/ `DomainEditor`（一个域）/ `ConfigField`（一行），
 * 本文件只负责「当前是哪一段」与「离开前确认」。
 */
import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { fetchConfigIndex } from '../../api/client';
import { DomainEditor } from './DomainEditor';
import { ModelPanel } from './model/ModelPanel';
import { ProvidersPanel } from './providers/ProvidersPanel';
import { SectionTree } from './SectionTree';
import { Tour } from './Tour';

/** 不在域注册表里的两个「设置专属」分区。 */
const EXTRA = new Set(['model', 'providers']);

interface Props {
  initialSection?: string | null;
  initialField?: string | null;
}

export function SettingsTab({ initialSection, initialField }: Props) {
  const [params, setParams] = useSearchParams();
  const index = useQuery({ queryKey: ['config', 'index'], queryFn: fetchConfigIndex });
  const domains = index.data?.domains ?? [];
  const known = new Set([...domains.map((item) => item.domain), ...EXTRA]);
  // 分区只认 URL（`section`，旧链接还有 `domain`），`initialSection` 只用于首帧：
  // 否则「点了左树却没反应」会取决于父组件有没有跟着重渲染。
  const fromUrl = params.get('section') ?? params.get('domain');
  const candidate = fromUrl || initialSection;
  const section = candidate && known.has(candidate) ? candidate : 'model';
  const session = params.get('session') ?? '';

  // 子组件只上报「有没有未保存改动」；离开确认在这里做，跳转路径只有一处
  const dirty = useRef(false);
  const [, forceRender] = useState(0);
  const markDirty = (value: boolean) => {
    if (dirty.current === value) return;
    dirty.current = value;
    forceRender((n) => n + 1);
  };

  function select(next: string) {
    if (next === section) return;
    if (dirty.current && !window.confirm('有未保存的改动，离开将丢弃它们。确定离开？')) return;
    const search = new URLSearchParams(params);
    search.set('section', next);
    // `domain` 是旧参数：留着会让刷新又跳回旧分区
    search.delete('domain');
    search.delete('field');
    dirty.current = false;
    setParams(search, { replace: true });
  }

  return (
    <div className="panel cfg-panel settings-panel">
      <div className="cfg-header">
        <div>
          <h2>设置</h2>
          <p className="hint">
            模型与网关决定「用什么」；运行策略决定「怎么跑」。值改完立刻写入，
            右侧徽标说明它现在的来源与生效时机。
          </p>
        </div>
        {index.data && (
          <p className="cfg-storage mono">
            {index.data.mode === 'file' ? 'file 模式（不读写配置库）' : index.data.db_path}
          </p>
        )}
      </div>

      {index.isError && (
        <p className="error-text">读取配置分区失败：后端可能还是旧版本，刷新重试。</p>
      )}

      <Tour />

      <div className="cfg-body">
        <SectionTree domains={domains} section={section} onSelect={select} />
        <div className="cfg-main">
          {section === 'model' && (
            <ModelPanel
              session={session}
              initialField={initialField}
              onDirtyChange={markDirty}
            />
          )}
          {section === 'providers' && <ProvidersPanel onDirtyChange={markDirty} />}
          {section !== 'model' && section !== 'providers' && (
            <DomainEditor domain={section} onDirtyChange={markDirty} />
          )}
        </div>
      </div>
    </div>
  );
}
