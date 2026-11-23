import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import YAML from 'yaml';
import { defineConfig } from 'vitest/config';

// 与后端共用 config/gates.yaml：阈值只在一处维护。
const gatesPath = fileURLToPath(new URL('../config/gates.yaml', import.meta.url));
const frontendGates = (
  YAML.parse(readFileSync(gatesPath, 'utf8')) as {
    frontend_gates?: { coverage_fail_under?: number };
  }
).frontend_gates ?? {};
const failUnder = frontendGates.coverage_fail_under ?? 60;

export default defineConfig({
  test: {
    environment: 'node',
    coverage: {
      provider: 'v8',
      reporter: ['text'],
      include: [
        'src/api/**/*.ts',
        'src/components/**/*.tsx',
        'src/workspace/**/*.tsx',
      ],
      thresholds: {
        lines: failUnder,
        functions: failUnder,
        branches: failUnder,
        statements: failUnder,
      },
    },
  },
});
