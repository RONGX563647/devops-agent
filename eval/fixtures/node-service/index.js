/** 最小 HTTP 服务：两个路由，用于评测「读代码 + 跑测试」。 */
const http = require('node:http');

const PORT = Number(process.env.PORT || 3000);

/** 把两个数字相加；非数字返回 null。 */
function add(left, right) {
  const a = Number(left);
  const b = Number(right);
  if (Number.isNaN(a) || Number.isNaN(b)) return null;
  return a + b;
}

/** 健康检查：返回 ok 与当前时间戳。 */
function health() {
  return { status: 'ok', now: new Date().toISOString() };
}

function createServer() {
  return http.createServer((req, res) => {
    res.setHeader('content-type', 'application/json; charset=utf-8');
    if (req.url === '/health') {
      res.end(JSON.stringify(health()));
      return;
    }
    if (req.url && req.url.startsWith('/add')) {
      const query = new URL(req.url, `http://localhost:${PORT}`).searchParams;
      const sum = add(query.get('a'), query.get('b'));
      res.statusCode = sum === null ? 400 : 200;
      res.end(JSON.stringify({ sum }));
      return;
    }
    res.statusCode = 404;
    res.end(JSON.stringify({ error: 'not found' }));
  });
}

module.exports = { add, health, createServer };

if (require.main === module) {
  createServer().listen(PORT, () => {
    console.log(`listening on ${PORT}`);
  });
}
