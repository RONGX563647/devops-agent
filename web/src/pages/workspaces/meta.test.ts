/** 工作区文案工具：错误码翻译与路径压缩。
 *
 * 这些函数是「后端只说错误码」和「用户要看到下一步」之间唯一的翻译层，
 * 所以覆盖的是映射本身，而不是渲染。
 */
import { describe, expect, it } from 'vitest';
import { ApiError } from '../../api/http';
import { basename, errorText, repoName, shortPath, sourceDetail, sourceLabel } from './meta';

describe('工作区文案', () => {
  it('已知错误码翻成能指导下一步的中文', () => {
    const exc = new ApiError('请求失败 400', 400, 'workspace_path_taken');
    expect(errorText(exc)).toContain('已被其他工作区引用');
  });

  it('未知错误码回落到原始 message，不吞掉信息', () => {
    const exc = new ApiError('请求失败 500', 500, 'some_new_code');
    expect(errorText(exc)).toBe('请求失败 500');
  });

  it('结构化 detail 带上脏文件数量', () => {
    const exc = new ApiError('请求失败 409', 409, {
      code: 'workspace_dirty',
      message: 'dirty',
      changed_files: ['a.ts', 'b.ts', 'c.ts', 'd.ts', 'e.ts', 'f.ts'],
    });
    const text = errorText(exc);
    expect(text).toContain('未提交改动');
    expect(text).toContain('等 6 个文件');
  });

  it('404 提示版本断层而不是「请重试」', () => {
    expect(errorText(new ApiError('请求失败 404', 404, null))).toContain('重启服务');
  });

  it('非 API 异常原样透出', () => {
    expect(errorText(new Error('network down'))).toBe('network down');
    expect(errorText('boom')).toBe('操作失败，请重试');
  });

  it('路径压缩只留末段，Git 地址取仓库名', () => {
    expect(shortPath('C:\\work\\code\\demo', 2)).toBe('code/demo');
    expect(basename('/srv/code/webpanel/')).toBe('webpanel');
    expect(repoName('https://github.com/acme/demo.git')).toBe('demo');
    expect(repoName('git@github.com:acme/demo.git')).toBe('demo');
    expect(sourceDetail({ source: 'git', branch: 'main' })).toBe('受管克隆 · main');
    expect(sourceDetail({ source: 'local', branch: '' })).toBe('原地引用');
    // 工作树是第三种来源：既不是克隆也不是原地引用，分支一定带出来
    expect(sourceLabel({ source: 'worktree' })).toBe('本地工作树');
    expect(sourceDetail({ source: 'worktree', branch: 'agent/demo-1a2b' })).toBe(
      '工作树 · agent/demo-1a2b',
    );
    expect(sourceDetail({ source: 'worktree', branch: '' })).toBe('工作树');
  });

  it('工作区目录丢了给可执行的下一步，而不是只说失败', () => {
    const text = errorText(new ApiError('conflict', 409, 'workspace_root_missing'));
    expect(text).toContain('目录不在了');
    expect(text).toContain('删除这个工作区');
  });
});
