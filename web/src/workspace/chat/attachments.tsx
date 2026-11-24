/** 附件的体积与状态文案。 */

import type { Attachment } from '../../types';

/** 文件大小的人类可读形式：附件动辄几 MB，纯字节数看不出来体量。 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 附件芯片右侧的计量文案：上传中 / 解析失败 / 体积与字数。 */
export function attachMeta(item: Attachment): string {
  if (item.status === 'uploading') return '上传中…';
  if (item.status === 'error') return '无法解析';
  const truncated = item.truncated ? '（已截断）' : '';
  return `${formatSize(item.size)} · ${item.chars} 字${truncated}`;
}
