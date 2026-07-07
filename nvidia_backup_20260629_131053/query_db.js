const initSqlJs = require('sql.js');
const fs = require('fs');
const path = require('path');

(async () => {
  const SQL = await initSqlJs();
  const dbPath = '/root/wrapper/nvidia/metrics.db';
  if (!fs.existsSync(dbPath)) {
    console.log("DB does not exist at " + dbPath);
    return;
  }
  const buf = fs.readFileSync(dbPath);
  const db = new SQL.Database(buf);

  console.log("--- Model Status ---");
  const stmt = db.prepare("SELECT * FROM model_status");
  while (stmt.step()) {
    console.log(stmt.getAsObject());
  }
  stmt.free();

  db.close();
})();
