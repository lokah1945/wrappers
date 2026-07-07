const sqlite3 = require('sqlite3').verbose();
const db = new sqlite3.Database('/root/.hermes/profiles/ilma/vane/data/db.sqlite');

db.all("SELECT name FROM sqlite_master WHERE type='table'", [], (err, rows) => {
  if (err) {
    console.error(err);
    return;
  }
  console.log("Tables:", rows);
  
  // Dump configurations table
  db.all("SELECT * FROM sqlite_master WHERE type='table'", [], (err2, schemas) => {
    schemas.forEach(s => {
      console.log(`\nTable Schema: ${s.name}`);
      console.log(s.sql);
    });
    
    // Dump actual settings if they exist
    db.all("SELECT * FROM model_providers", [], (err3, providers) => {
      if (!err3) {
        console.log("\nProviders:", providers);
      }
      db.all("SELECT * FROM models", [], (err4, models) => {
        if (!err4) {
          console.log("\nModels:", models);
        }
        db.close();
      });
    });
  });
});
