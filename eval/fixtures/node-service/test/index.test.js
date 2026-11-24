const test = require('node:test');
const assert = require('node:assert');

const { add, health } = require('../index.js');

test('add 相加两个数字', () => {
  assert.strictEqual(add(1, 2), 3);
  assert.strictEqual(add(-1, 1), 0);
});

test('add 接受字符串形式的数字', () => {
  assert.strictEqual(add('2', '3'), 5);
});

test('add 遇到非数字返回 null', () => {
  assert.strictEqual(add('a', 1), null);
});

test('health 返回 ok', () => {
  assert.strictEqual(health().status, 'ok');
});
