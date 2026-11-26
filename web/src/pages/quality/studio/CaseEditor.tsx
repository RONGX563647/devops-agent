/**
 * 用例编辑器：直接改这条用例的 JSON。
 *
 * 为什么给 JSON 而不是表单：`expected` 有 12 个字段、`turns` 支持确认轮，表单
 * 化会变成二十个控件，而真正需要改的往往只是一句 `must_contain`。JSON 至少让
 * 「改了什么」一眼可见，diff 也干净。
 *
 * 两道校验都在服务端：本地只挡「不是 JSON」，结构合法性由 `parse_case` +
 * 单条静态检查判定 —— 前端复制一份校验逻辑，两边迟早不一致。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  deleteOpenJudgeCase,
  fetchOpenJudgeCaseDocument,
  restoreOpenJudgeCase,
  saveOpenJudgeCase,
} from '../../../api/client';

const META_KEYS = new Set(['_source', '_edited', '_deleted_from_repo']);

/** 去掉服务端附加的下划线字段：把它们写回题库会让 `parse_case` 报未知字段。 */
function editable(body: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(body).filter(([key]) => !META_KEYS.has(key)));
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === 'string' ? err.detail : `请求失败 ${err.status}`;
  }
  return err instanceof Error ? err.message : '请求失败';
}

interface CaseEditorProps {
  caseId: string;
  onClose: () => void;
}

export function CaseEditor({ caseId, onClose }: CaseEditorProps) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ['oj-case-doc', caseId],
    queryFn: () => fetchOpenJudgeCaseDocument(caseId),
  });
  const [draft, setDraft] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 草稿优先：保存成功后服务端会返回新文档，但用户可能已经在继续打字，
  // 用「有草稿就用草稿」避免把输入覆盖掉。
  const text = draft ?? (query.data ? JSON.stringify(editable(query.data.case), null, 2) : '');

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['oj-case-doc', caseId] });
    void qc.invalidateQueries({ queryKey: ['oj-library'] });
    void qc.invalidateQueries({ queryKey: ['oj-settings'] });
  };

  const save = useMutation({
    mutationFn: () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch (err) {
        throw new Error(`JSON 解析失败：${(err as Error).message}`, { cause: err });
      }
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('用例必须是一个 JSON 对象');
      }
      return saveOpenJudgeCase(caseId, parsed as Record<string, unknown>);
    },
    onSuccess: (payload) => {
      setDraft(JSON.stringify(editable(payload.case), null, 2));
      setNotice('已保存到本地覆盖层（不影响 git 里的题库）');
      refresh();
    },
    onError: (err) => setNotice(errorMessage(err)),
  });

  const remove = useMutation({
    mutationFn: () => deleteOpenJudgeCase(caseId),
    onSuccess: () => {
      setNotice('已从题库隐藏（可恢复）');
      refresh();
    },
    onError: (err) => setNotice(errorMessage(err)),
  });

  const restore = useMutation({
    mutationFn: () => restoreOpenJudgeCase(caseId),
    onSuccess: (payload) => {
      setDraft(JSON.stringify(editable(payload.case), null, 2));
      setNotice('已恢复仓库版本');
      refresh();
    },
    onError: (err) => setNotice(errorMessage(err)),
  });

  if (query.isLoading) return <div className="muted">加载用例…</div>;
  if (query.error || !query.data) return <div className="muted">用例不可用</div>;

  const doc = query.data.case;
  const busy = save.isPending || remove.isPending || restore.isPending;
  const failed = save.isError || remove.isError || restore.isError;

  return (
    <div className="oj-editor">
      <div className="oj-editor-head">
        <h3>{caseId}</h3>
        <span className={doc._source === 'overlay' ? 'oj-badge overlay' : 'oj-badge'}>
          {doc._source === 'overlay' ? '本地改动' : '仓库原版'}
        </span>
        <button type="button" onClick={onClose}>
          关闭
        </button>
      </div>

      <textarea
        className="oj-editor-text"
        aria-label="用例 JSON"
        spellCheck={false}
        value={text}
        onChange={(event) => setDraft(event.target.value)}
        rows={22}
        style={{ maxHeight: '50vh' }}
      />

      <div className="oj-form-actions">
        <button type="button" onClick={() => save.mutate()} disabled={busy}>
          {save.isPending ? '保存中…' : '保存'}
        </button>
        <button type="button" onClick={() => restore.mutate()} disabled={busy}>
          恢复仓库版本
        </button>
        <button type="button" onClick={() => remove.mutate()} disabled={busy}>
          从题库隐藏
        </button>
        {notice && (
          <span className={failed ? 'oj-error' : 'muted'} role="status">
            {notice}
          </span>
        )}
      </div>

      <p className="muted">
        保存只写 `workspace/openjudge/cases/`；结构错误会整条拒绝，不会留下半截文件。
      </p>
    </div>
  );
}
