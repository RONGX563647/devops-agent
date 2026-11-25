/**
 * 评测模型配置卡：被测模型与裁判模型分开填。
 *
 * 为什么非得分两个：裁判与被测模型同源时，裁判对自己的回答更宽容（1–5 分制下
 * 「正确但略啰嗦」普遍拿 4 分），换一个裁判往往比换个被测模型更能暴露问题。
 * 过去换裁判只能改环境变量重启进程，代价高到没人会真去做 —— 这张卡就是为了
 * 把那件事压到一次点击。
 *
 * 空字符串表示「不覆盖」：下面实时显示它最终解析到的模型名，否则「我明明没填，
 * 为什么在跑另一个模型」无法解释。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  fetchOpenJudgeSettings,
  patchOpenJudgeSettings,
} from '../../../api/client';
import type { OjSettingsValues } from '../../../types';

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return typeof err.detail === 'string' ? err.detail : `请求失败 ${err.status}`;
  }
  return err instanceof Error ? err.message : '请求失败';
}

export function ModelCard() {
  const qc = useQueryClient();
  const query = useQuery({ queryKey: ['oj-settings'], queryFn: fetchOpenJudgeSettings });
  const [notice, setNotice] = useState<string | null>(null);

  const data = query.data;
  // 草稿优先、服务端次之：不把 query 数据 setState 进本地状态，否则「保存后
  // 重新拉取」会把用户正在输的字覆盖掉（也躲不掉 set-state-in-effect 的坑）。
  const [draft, setDraft] = useState<OjSettingsValues | null>(null);
  const values: OjSettingsValues = draft ?? data?.values ?? {
    target_model: '',
    judge_model: '',
    judge: false,
    concurrency: 2,
    timeout: 180,
  };
  const update = (patch: Partial<OjSettingsValues>) =>
    setDraft({ ...values, ...patch });

  const save = useMutation({
    mutationFn: () =>
      patchOpenJudgeSettings({
        target_model: values.target_model.trim(),
        judge_model: values.judge_model.trim(),
        judge: values.judge,
        concurrency: values.concurrency,
        timeout: values.timeout,
      }),
    onSuccess: (payload) => {
      setDraft(payload.values);
      setNotice(
        `已保存：被测 ${payload.effective.target_model || '—'} / 裁判 ${
          payload.effective.judge_model || '—'
        }`,
      );
      void qc.invalidateQueries({ queryKey: ['oj-settings'] });
    },
    onError: (err) => setNotice(errorMessage(err)),
  });

  const limits = data?.limits;
  const effective = data?.effective;
  const busy = query.isLoading || save.isPending;
  const failed = save.isError || query.isError;

  return (
    <section className="panel oj-model-card">
      <h3>评测模型</h3>
      <p className="muted">
        留空即「跟随默认」。被测模型只覆盖模型名，温度、窗口等参数仍走模型设置页。
      </p>

      <div className="oj-form-row">
        <label htmlFor="oj-target-model">被测模型</label>
        <input
          id="oj-target-model"
          value={values.target_model}
          placeholder={effective?.target_model || '跟随全局模型配置'}
          onChange={(event) => update({ target_model: event.target.value })}
          disabled={busy}
        />
        <span className="muted">实际生效：{effective?.target_model || '未配置'}</span>
      </div>

      <div className="oj-form-row">
        <label htmlFor="oj-judge-model">裁判模型</label>
        <input
          id="oj-judge-model"
          value={values.judge_model}
          placeholder={effective?.judge_model || '跟随被测模型'}
          onChange={(event) => update({ judge_model: event.target.value })}
          disabled={busy}
        />
        <span className="muted">实际生效：{effective?.judge_model || '未配置'}</span>
      </div>

      <div className="oj-form-row">
        <label htmlFor="oj-judge-default">默认启用裁判</label>
        <input
          id="oj-judge-default"
          type="checkbox"
          checked={values.judge}
          onChange={(event) => update({ judge: event.target.checked })}
          disabled={busy}
        />
        <span className="muted">关掉只跑规则门禁，不花裁判额度</span>
      </div>

      <div className="oj-form-row">
        <label htmlFor="oj-concurrency">并发</label>
        <input
          id="oj-concurrency"
          type="number"
          min={1}
          max={limits?.max_concurrency ?? 4}
          value={values.concurrency}
          onChange={(event) => update({ concurrency: Number(event.target.value) })}
          disabled={busy}
        />
        <label htmlFor="oj-timeout">单条超时（秒）</label>
        <input
          id="oj-timeout"
          type="number"
          min={limits?.min_timeout ?? 30}
          max={limits?.max_timeout ?? 900}
          step={30}
          value={values.timeout}
          onChange={(event) => update({ timeout: Number(event.target.value) })}
          disabled={busy}
        />
      </div>

      <div className="oj-form-actions">
        <button type="button" onClick={() => save.mutate()} disabled={busy}>
          {save.isPending ? '保存中…' : '保存配置'}
        </button>
        {notice && (
          <span className={failed ? 'oj-error' : 'muted'} role="status">
            {notice}
          </span>
        )}
      </div>
    </section>
  );
}
