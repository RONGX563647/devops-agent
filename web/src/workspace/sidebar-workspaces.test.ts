import { describe, expect, it } from 'vitest';
import type { WorkspaceSessionBoard } from '../types';
import { buildWorkspaceGroups, filterGroups, injectDraft, unboundSessions } from './sidebar-workspaces';
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
        ] as unknown as WorkspaceSessionBoard['items'],
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
      ] as unknown as WorkspaceSessionBoard['items'],
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

describe('injectDraft', () => {
  const groups = buildWorkspaceGroups(
    board({
      items: [
        {
          id: 'ws_a',
          name: 'webpanel',
          root_path: '/code/webpanel',
          status: 'active',
          session_count: 0,
          sessions: [],
        },
      ] as unknown as WorkspaceSessionBoard['items'],
    }),
  );

  it('草稿挂到所属工作区顶部，不计入服务端条数', () => {
    const merged = injectDraft(groups, { id: 'draft-1', workspaceId: 'ws_a', title: '新会话' });
    expect(merged[0].sessions[0]).toMatchObject({ id: 'draft-1', draft: true, title: '新会话' });
    expect(merged[0].count).toBe(0);
  });

  it('服务端已经有这条会话时不重复插入', () => {
    const existing = injectDraft(groups, { id: 'd1', workspaceId: 'ws_a', title: '新会话' });
    const again = injectDraft(existing, { id: 'd1', workspaceId: 'ws_a', title: '新会话' });
    expect(again[0].sessions).toHaveLength(1);
  });

  it('工作区不存在时草稿不出现（不凭空造分组）', () => {
    const merged = injectDraft(groups, { id: 'd1', workspaceId: 'ws_missing', title: '新会话' });
    expect(merged).toHaveLength(1);
    expect(merged[0].sessions).toEqual([]);
  });
});

describe('filterGroups', () => {
  const groups = buildWorkspaceGroups(
    board({
      items: [
        {
          id: 'ws_a',
          name: 'webpanel',
          root_path: '/code/webpanel',
          status: 'active',
          session_count: 2,
          sessions: [
            {
              session_id: 's1',
              title: '修 CI 流水线',
              last_at: '2026-11-28T10:00:00+00:00',
              run_count: 1,
              failed_count: 0,
              bound_at: '',
              bound_by: '',
            },
            {
              session_id: 's2',
              title: '已经归档的会话',
              last_at: '2026-11-27T10:00:00+00:00',
              run_count: 1,
              failed_count: 0,
              bound_at: '',
              bound_by: '',
              archived_at: '2026-11-27T11:00:00+00:00',
            },
          ],
        },
        {
          id: 'ws_old',
          name: 'old',
          root_path: '/code/old',
          status: 'archived',
          session_count: 1,
          sessions: [],
        },
      ] as unknown as WorkspaceSessionBoard['items'],
    }),
  );

  it('默认隐藏归档工作区与归档会话', () => {
    const visible = filterGroups(groups);
    expect(visible.map((group) => group.id)).toEqual(['ws_a']);
    expect(visible[0].sessions.map((item) => item.id)).toEqual(['s1']);
  });

  it('打开「显示已归档」后两者都回来', () => {
    const visible = filterGroups(groups, { showArchived: true });
    expect(visible.map((group) => group.id)).toEqual(['ws_a', 'ws_old']);
    expect(visible[0].sessions.map((item) => item.id)).toEqual(['s1', 's2']);
  });

  it('搜索命中工作区名时保留它全部会话', () => {
    const visible = filterGroups(groups, { query: 'web' });
    expect(visible[0].sessions.map((item) => item.id)).toEqual(['s1']);
  });

  it('一条都不匹配的工作区被收起', () => {
    expect(filterGroups(groups, { query: '不存在' })).toEqual([]);
  });
});
