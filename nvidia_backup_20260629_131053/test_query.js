const initSqlJs = require('sql.js');
const fs = require('fs');

(async () => {
  const SQL = await initSqlJs();
  const dbPath = '/root/wrapper/nvidia/metrics.db';
  if (!fs.existsSync(dbPath)) return;
  const buf = fs.readFileSync(dbPath);
  const db = new SQL.Database(buf);

  console.log("--- API Key Usage Distribution ---");
  const stmt = db.prepare("SELECT key_label, COUNT(*) as count, SUM(status_code = 200) as success_200, SUM(status_code = 429) as rl_429 FROM requests GROUP BY key_label ORDER BY key_label ASC");
  while (stmt.step()) {
    console.log(stmt.getAsObject());
  }
  stmt.free();
  db.close();
})();
