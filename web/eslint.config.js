import { readFileSync } from 'node:fs';
import { URL, fileURLToPath } from 'node:url';
import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import YAML from 'yaml';
import tseslint from 'typescript-eslint';

// 阈值与后端共用 config/gates.yaml，避免前端再维护一份数字。
const gatesPath = fileURLToPath(new URL('../config/gates.yaml', import.meta.url));
const frontendGates = YAML.parse(readFileSync(gatesPath, 'utf8')).frontend_gates ?? {};

export default tseslint.config(
  { ignores: ['dist/**', 'coverage/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: { ...reactHooks.configs.recommended.rules },
  },
  {
    files: ['**/*.ts'],
    rules: {
      // JSX 条件渲染会被 complexity 计入，故只对纯逻辑 .ts 文件卡阈值。
      complexity: ['error', frontendGates.complexity_threshold ?? 10],
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/**/*.test.{ts,tsx}'],
    rules: {
      // 新文件硬上限；存量超标文件在下方显式豁免，拆分后逐条删除。
      'max-lines': [
        'error',
        {
          max: frontendGates.max_lines ?? 500,
          skipBlankLines: true,
          skipComments: true,
        },
      ],
    },
  },
  {
    files: [
      'src/types.ts',
      'src/api/useRunStream.ts',
      'src/pages/EvalBatches.tsx',
      'src/workspace/TraceList.tsx',
      'src/workspace/ChatTab.tsx',
      'src/workspace/store.ts',
      'src/workspace/trace.ts',
      'src/workspace/ModelConfigPopover.tsx',
    ],
    rules: { 'max-lines': 'off' },
  },
);
