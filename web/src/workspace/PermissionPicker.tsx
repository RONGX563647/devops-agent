/** 权限档位选择器：底部按钮 + 向上弹层。
 *
 * 档位不是「安全等级」，而是「默认放行到哪一层 + 越界怎么办」，所以每项都带
 * 一行说明和兜底动作标签 —— 只说「只读/标准/放开」用户没法判断该选哪个。
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { fetchPermissions } from '../api/client';
import type { PermissionKey, PermissionMeta } from '../types';

/** 后端不可用时的兜底列表：至少让界面能用，且不含 unrestricted。 */
export const FALLBACK_PROFILES: PermissionMeta[] = [
  {
    key: 'readonly',
    label: '只读',
    note: '只能读文件、跑只读命令；被拦的调用直接拒绝。',
    fallback: 'deny',
    write: false,
  },
  {
    key: 'workspace',
    label: '工作区可写',
    note: '可在工作区内建/删/改文件；越界会先弹卡片问你。',
    fallback: 'ask',
    write: true,
  },
  {
    key: 'execute',
    label: '可执行',
    note: '工作区权限 + 白名单命令（pytest / npm run 等）；越界仍会先问。',
    fallback: 'ask',
    write: true,
  },
  {
    key: 'auto',
    label: '无人值守',
    note: '给定时任务用：工作区内可读写，越界一律拒绝且不弹确认。',
    fallback: 'deny',
    write: true,
  },
];

/** 兜底动作的中文短标签。 */
export const FALLBACK_LABEL: Record<PermissionMeta['fallback'], string> = {
  deny: '越界即拒',
  ask: '越界先问',
  hard_deny: '硬规则拦截',
};

interface Props {
  current: PermissionKey;
  onChange: (key: PermissionKey) => void;
  disabled?: boolean;
  /** 运行中切档只影响下一轮：档位已随发起请求锁进当前 run。 */
  deferred?: boolean;
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 12 12"
      fill="none"
      aria-hidden="true"
      style={{ transform: open ? 'rotate(180deg)' : undefined }}
    >
      <path
        d="M3 7.5 6 4.5l3 3"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function PermissionPicker({ current, onChange, disabled, deferred }: Props) {
  const [open, setOpen] = useState(false);
  const [pendingKey, setPendingKey] = useState<PermissionKey | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const { data } = useQuery({
    queryKey: ['permissions'],
    queryFn: fetchPermissions,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  const profiles = data?.available?.length ? data.available : FALLBACK_PROFILES;
  const active = profiles.find((item) => item.key === current) ?? profiles[0];

  // 点外面关掉：弹层贴在输入框上，误触范围要收住
  useEffect(() => {
    if (!open) return undefined;
    function onDocClick(event: MouseEvent) {
      if (!wrapRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setPendingKey(null);
      }
    }
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  function pick(item: PermissionMeta) {
    // 放开档跳过逐次确认，选错代价最大，所以先就地确认一次
    if (item.key === 'unrestricted') {
      setPendingKey(item.key);
      return;
    }
    onChange(item.key);
    setOpen(false);
  }

  return (
    <div className="perm-wrap" ref={wrapRef}>
      <button
        type="button"
        className={`perm-btn${open ? ' is-open' : ''}`}
        aria-label="选择权限档位"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        title={active?.note}
      >
        <span className={`perm-dot risk-${active?.fallback ?? 'deny'}`} aria-hidden="true" />
        <span className="perm-label">{active?.label ?? '权限'}</span>
        {deferred && <span className="perm-deferred mono">下一轮生效</span>}
        <ChevronIcon open={open} />
      </button>
      {open && (
        <div className="perm-menu" role="menu" aria-label="权限档位">
          {pendingKey ? (
            <div className="perm-confirm">
              <p className="perm-confirm-text">
                放开档会跳过所有逐次确认，只剩一层规则拦截。仅建议在本地或一次性环境使用。
                {deferred && '当前有任务在跑，切换将在下一轮生效。'}
              </p>
              <div className="perm-confirm-actions">
                <button type="button" className="ghost-btn" onClick={() => setPendingKey(null)}>
                  取消
                </button>
                <button
                  type="button"
                  className="danger-btn"
                  onClick={() => {
                    onChange('unrestricted');
                    setPendingKey(null);
                    setOpen(false);
                  }}
                >
                  仍要切换
                </button>
              </div>
            </div>
          ) : (
            profiles.map((item) => (
              <button
                type="button"
                key={item.key}
                role="menuitemradio"
                aria-checked={item.key === current}
                className={`perm-item${item.key === current ? ' is-active' : ''}`}
                onClick={() => pick(item)}
              >
                <span className="perm-item-head">
                  <span className={`perm-dot risk-${item.fallback}`} aria-hidden="true" />
                  <span className="perm-item-label">{item.label}</span>
                  {item.write && <span className="perm-write-tag">可写</span>}
                  {item.key === current && <span className="perm-check">✓</span>}
                </span>
                <span className="perm-item-note">{item.note}</span>
                <span className="perm-item-fallback mono">{FALLBACK_LABEL[item.fallback]}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
