import { describe, expect, it } from 'vitest';
import type { WorkspaceSessionBoard } from '../types';
import { buildWorkspaceGroups, unboundSessions } from './sidebar-workspaces';
import type { SessionItem } from './sidebar-utils';

const long = '一二三四五六七八九十一二三四五六七八九十超出部分';

function board(overrides: Partial<WorkspaceSessionBoard> = {}): WorkspaceSessionBoard {
  return { items: [], unbound: [], ...overrides };
}

function session(id: string, lastAt: string, title = id): SessionItem {
  return { id, title, lastAt, runCount: 1, failedCount: 0 };
}

describe('buildWorkspaceGroups', () => {
  it('保留后端顺序，把会话投影成侧栏条目', () => {
    const groups = buildWorkspaceGroups(
      board({
        items: [
          {
            id: 'ws_a',
            name: 'webpanel',
            root_path: 'E:/code/webpanel',
            status: 'active',
            session_count: 4,
            sessions: [
              {
                session_id: 's1',
                title: long,
                last_at: '2026-11-28T10:00:00+00:00',
                run_count: 2,
                failed_count: 1,
                bound_at: '2026-11-28T09:00:00+00:00',
                bound_by: 'local',
              },
            ],
          },
          {
            id: 'ws_b',
            name: 'old',
            root_path: 'E:/code/old',
            status: 'archived',
            session_count: 0,
            sessions: [],
          },
        ] as WorkspaceSessionBoard['items'],
      }),
    );
    expect(groups.map((group) => group.id)).toEqual(['ws_a', 'ws_b']);
    expect(groups[0].count).toBe(4);
    expect(groups[0].sessions[0].title).toBe(`${long.slice(0, 20)}…`);
    expect(groups[0].sessions[0].failedCount).toBe(1);
    expect(groups[1].status).toBe('archived');
  });

  it('形状不对的分组直接跳过，不让半截数据把侧栏渲染崩', () => {
    const groups = buildWorkspaceGroups({
      items: [{ id: 'ws_x' } as never],
      unbound: [],
    });
    expect(groups).toEqual([]);
  });

  it('没有 board（请求失败 / 老后端）时返回空数组', () => {
    expect(buildWorkspaceGroups(undefined)).toEqual([]);
  });
});

describe('unboundSessions', () => {
  it('后端未归属与本地草稿合并，已进入分组的会话被剔除', () => {
    const data = board({
      items: [
        {
          id: 'ws_a',
          name: 'webpanel',
          root_path: 'E:/code/webpanel',
          status: 'active',
          session_count: 1,
          sessions: [
            {
              session_id: 'bound',
              title: '已归属',
              last_at: '2026-11-28T10:00:00+00:00',
              run_count: 1,
              failed_count: 0,
              bound_at: '2026-11-28T09:00:00+00:00',
              bound_by: 'local',
            },
          ],
        },
      ] as WorkspaceSessionBoard['items'],
      unbound: [
        {
          session_id: 'loose',
          title: '未归属',
          last_at: '2026-11-28T11:00:00+00:00',
          run_count: 2,
          failed_count: 0,
          bound_at: '',
          bound_by: '',
        },
      ],
    });
    const merged = unboundSessions(data, [
      session('bound', '2026-11-28T12:00:00+00:00'),
      session('draft', '2026-11-28T08:00:00+00:00', '草稿'),
    ]);
    expect(merged.map((item) => item.id)).toEqual(['loose', 'draft']);
  });

  it('同一会话优先用后端的标题与运行数，本地只补轮次', () => {
    const data = board({
      unbound: [
        {
          session_id: 's1',
          title: '后端标题',
          last_at: '2026-11-28T10:00:00+00:00',
          run_count: 3,
          failed_count: 1,
          bound_at: '',
          bound_by: '',
        },
      ],
    });
    const [item] = unboundSessions(data, [
      { ...session('s1', '2026-11-28T12:00:00+00:00', '本地标题'), messageCount: 5 },
    ]);
    expect(item.title).toBe('后端标题');
    expect(item.runCount).toBe(3);
    expect(item.messageCount).toBe(5);
    expect(item.lastAt).toBe('2026-11-28T10:00:00+00:00');
  });
});
