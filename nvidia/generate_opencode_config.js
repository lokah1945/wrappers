const fs = require('fs');

async function main() {
  const cap = await fetch('http://localhost:9100/v1/capabilities').then(r => r.json());
  
  const opencodeConfig = {
    "provider": "wrapper-nvidia",
    "base_url": "http://localhost:9100/v1",
    "api_key": "optional",
    "models": {}
  };
  
  const openclawConfig = {
    "provider": "wrapper-nvidia",
    "base_url": "http://localhost:9100/v1",
    "api_key": "optional",
    "api": "openai-completions", // Chat Completions api required for OpenClaw
    "models": {}
  };

  for (const m of cap.models) {
    if (m.type === 'chat' || m.type === 'vision_chat' || m.type === 'parse') {
      opencodeConfig.models[m.id] = {};
      
      openclawConfig.models[m.id] = { compaction_reserve: 1024 };
    }
  }
  
  fs.writeFileSync('opencode_provider.json', JSON.stringify(opencodeConfig, null, 2));
  fs.writeFileSync('openclaw_provider.json', JSON.stringify(openclawConfig, null, 2));
  console.log('Configs generated successfully.');
}
main();
