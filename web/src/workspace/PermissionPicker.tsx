/** 权限档位选择器：两维（能力范围 / 打扰程度）+ 实际判定预览。
 *
 * 为什么不是「四选一」：一个档位以前捆绑了 mode + 工具集 + 白名单 + 兜底动作，
 * 于是「工作区可写但不问」这种最普通的需求表达不出来，用户只能反复点确认。
 * 现在两个维度分开选，选中哪一格就对应一个预设。
 *
 * 预览不放「越界才问」这种模糊话：三行清单由后端拿真实引擎跑取样命令得出，
 * 用户不必试一次才知道自己选了什么。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { createPermissionRule, deletePermissionRule, fetchPermissions } from '../api/client';
import type {
  PermissionApprovals,
  PermissionKey,
  PermissionMeta,
  PermissionScope,
} from '../types';

/** 后端不可用时的兜底列表：至少让界面能用，且不含全放开档。 */
export const FALLBACK_PROFILES: PermissionMeta[] = [
  meta('readonly', '只读', 'readonly', 'never', ['ls -la'], [], ['pytest -q'], '只读浏览：不下发写工具，越界命令直接拒绝。'),
  meta('workspace', '工作区可写', 'workspace', 'on-risk', ['ls -la', 'pytest -q'], ['git commit'], [], '工作区可读写；测试/构建命令直接执行；git 与其他命令逐次询问。'),
  meta('cautious', '逐个确认', 'workspace', 'always', ['ls -la'], ['pytest -q'], [], '有写能力，但每个动作都确认。'),
  meta('unattended', '无人值守', 'workspace', 'never', ['ls -la', 'pytest -q'], [], ['git commit'], '读与测试/构建直接执行，其余直接拒绝。'),
];

/** 后端不可用时的维度兜底：与 governance/rules.py 的 SCOPES 一致。 */
const FALLBACK_SCOPES: PermissionScope[] = ['readonly', 'workspace', 'container'];
const FALLBACK_APPROVALS: PermissionApprovals[] = ['always', 'on-risk', 'never'];

function meta(
  key: PermissionKey,
  label: string,
  scope: PermissionScope,
  approvals: PermissionApprovals,
  autoAllow: string[],
  willAsk: string[],
  willDeny: string[],
  note: string,
): PermissionMeta {
  return {
    key,
    label,
    note,
    scope,
    approvals,
    combo: `${scope}+${approvals}`,
    tools: scope === 'readonly' ? ['bash', 'read', 'glob', 'grep'] : ['bash', 'read', 'glob', 'grep', 'write', 'edit'],
    write: scope !== 'readonly',
    auto_allow: autoAllow,
    will_ask: willAsk,
    will_deny: willDeny,
  };
}

export const SCOPE_LABEL: Record<PermissionScope, string> = {
  readonly: '只读',
  workspace: '工作区',
  container: '不限目录',
};

export const APPROVALS_LABEL: Record<PermissionApprovals, string> = {
  always: '逐个确认',
  'on-risk': '遇到风险才问',
  never: '不再询问',
};

/** 能力范围的一行说明：只说「只读/工作区」用户不知道边界在哪。 */
const SCOPE_HINT: Record<PermissionScope, string> = {
  readonly: '不下发写工具',
  workspace: '可读写当前工作区',
  container: '不受工作目录限制',
};

const APPROVALS_HINT: Record<PermissionApprovals, string> = {
  always: '每个动作都问一次',
  'on-risk': '白名单命令直接跑',
  never: '越界直接拒绝',
};

interface Props {
  current: PermissionKey;
  onChange: (key: PermissionKey) => void;
  disabled?: boolean;
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

/** 三行预览：不问什么 / 会问什么 / 什么被拒。 */
function Preview({ item }: { item: PermissionMeta }) {
  // 老后端（升级期间的滚动发布）没有这三项：缺了就当空列表，别让整块界面炸掉
  const rows = [
    { tag: '不问', cls: 'ok', items: item.auto_allow ?? [] },
    { tag: '会问', cls: 'ask', items: item.will_ask ?? [] },
    { tag: '拒绝', cls: 'no', items: item.will_deny ?? [] },
  ];
  return (
    <div className="perm-preview">
      {rows.map((row) => (
        <p key={row.tag} className="perm-preview-row">
          <span className={`perm-preview-tag ${row.cls}`}>{row.tag}</span>
          <span className="perm-preview-items">
            {row.items.length > 0 ? row.items.slice(0, 4).join(' · ') : '—'}
          </span>
        </p>
      ))}
    </div>
  );
}

export function PermissionPicker({ current, onChange, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [pendingKey, setPendingKey] = useState<PermissionKey | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ['permissions'],
    queryFn: fetchPermissions,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  const profiles = data?.available?.length ? data.available : FALLBACK_PROFILES;
  const active = profiles.find((item) => item.key === current) ?? profiles[0];
  // 维度取服务端声明的全量而不是「开放档位里出现过的值」：未开放的组合要显示成
  // 置灰的一格（用户看得见「有这个概念，但这一档不给」），而不是整行少一格。
  const scopes = data?.scopes?.length ? data.scopes : FALLBACK_SCOPES;
  const levels = data?.approvals?.length ? data.approvals : FALLBACK_APPROVALS;
  const rules = data?.rules ?? [];
  const fatigue = data?.fatigue?.[0];

  const remove = useMutation({
    mutationFn: (rule: { tool: string; pattern: string | null }) =>
      deletePermissionRule(rule.tool, rule.pattern),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['permissions'] });
    },
  });

  const addRule = useMutation({
    mutationFn: (prefix: string) =>
      createPermissionRule({ tool: fatigue?.tool ?? 'Bash', pattern: `${prefix}:*` }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['permissions'] });
    },
  });

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

  function combo(scope: PermissionScope, level: PermissionApprovals): PermissionMeta | undefined {
    return profiles.find((item) => item.scope === scope && item.approvals === level);
  }

  function pick(target: PermissionMeta | undefined) {
    if (!target) return;
    // 不限目录档跳过逐次确认，选错代价最大，所以先就地确认一次
    if (target.scope === 'container') {
      setPendingKey(target.key);
      return;
    }
    onChange(target.key);
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
        <span className={`perm-dot risk-${active?.scope ?? 'workspace'}`} aria-hidden="true" />
        <span className="perm-label">{active?.label ?? '权限'}</span>
        <ChevronIcon open={open} />
      </button>
      {open && (
        <div className="perm-menu perm-menu-wide" role="menu" aria-label="权限档位">
          {pendingKey ? (
            <div className="perm-confirm">
              <p className="perm-confirm-text">
                这一档不受工作目录限制、也不再逐次确认，只剩内置规则拦截。仅建议在本地或一次性环境使用。
              </p>
              <div className="perm-confirm-actions">
                <button type="button" className="ghost-btn" onClick={() => setPendingKey(null)}>
                  取消
                </button>
                <button
                  type="button"
                  className="danger-btn"
                  onClick={() => {
                    onChange(pendingKey);
                    setPendingKey(null);
                    setOpen(false);
                  }}
                >
                  仍要切换
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="perm-row">
                <span className="perm-row-label">能力范围</span>
                <div className="perm-seg">
                  {scopes.map((scope) => {
                    const target = combo(scope, active?.approvals ?? 'on-risk');
                    return (
                      <button
                        key={scope}
                        type="button"
                        role="menuitemradio"
                        aria-checked={active?.scope === scope}
                        disabled={!target}
                        title={target ? SCOPE_HINT[scope] : '该组合没有实现（只读档没有「什么都问」的形态）'}
                        className={`perm-seg-item${active?.scope === scope ? ' is-active' : ''}`}
                        onClick={() => pick(target)}
                      >
                        {SCOPE_LABEL[scope]}
                      </button>
                    );
                  })}
                </div>
              </div>
              <div className="perm-row">
                <span className="perm-row-label">打扰程度</span>
                <div className="perm-seg">
                  {levels.map((level) => {
                    const target = combo(active?.scope ?? 'workspace', level);
                    return (
                      <button
                        key={level}
                        type="button"
                        role="menuitemradio"
                        aria-checked={active?.approvals === level}
                        disabled={!target}
                        title={APPROVALS_HINT[level]}
                        className={`perm-seg-item${active?.approvals === level ? ' is-active' : ''}`}
                        onClick={() => pick(target)}
                      >
                        {APPROVALS_LABEL[level]}
                      </button>
                    );
                  })}
                </div>
              </div>
              {active && <p className="perm-item-note">{active.note}</p>}
              {active && <Preview item={active} />}
              {fatigue && (
                <div className="perm-fatigue">
                  <span className="perm-preview-tag ask">反复询问</span>
                  <span>
                    <code className="mono">{fatigue.prefix}</code> 已被问 {fatigue.count} 次
                  </span>
                  <button
                    type="button"
                    className="ghost-btn perm-fatigue-btn"
                    disabled={addRule.isPending}
                    onClick={() => addRule.mutate(fatigue.prefix.replace(':*', ''))}
                  >
                    以后都允许
                  </button>
                </div>
              )}
              {rules.length > 0 && (
                <div className="perm-rules">
                  <span className="perm-row-label">已沉淀规则</span>
                  <ul className="perm-rule-list">
                    {rules.map((rule) => (
                      <li key={`${rule.tool}:${rule.pattern}`} className="perm-rule">
                        <code className="mono">{rule.pattern ?? rule.tool}</code>
                        <span className="perm-rule-scope">{rule.scope}</span>
                        <button
                          type="button"
                          className="perm-rule-del"
                          aria-label={`撤销规则 ${rule.pattern ?? rule.tool}`}
                          onClick={() => remove.mutate({ tool: rule.tool, pattern: rule.pattern })}
                        >
                          ×
                        </button>
                      </li>
                    ))}
                  </ul>
                  {data?.rules_file && <p className="perm-rule-file mono">{data.rules_file}</p>}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
