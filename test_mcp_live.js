const { spawn } = require('child_process');

const token = 'a4588957-2e2d-49fb-b9f8-57edb82bc67c';

console.log('[*] Starting Bright Data MCP process...');

const proc = spawn('/Users/Breezy/.nvm/versions/node/v20.20.0/bin/npx', ['-y', '@brightdata/mcp'], {
  env: {
    ...process.env,
    BRIGHTDATA_API_TOKEN: token,
    API_TOKEN: token
  }
});

let buffer = '';

proc.stdout.on('data', (data) => {
  const str = data.toString();
  console.log('[+] Received from MCP server:', str);
  buffer += str;
  
  if (buffer.includes('"capabilities"')) {
    console.log('[*] Initialized! Requesting tools list...');
    const toolsReq = JSON.stringify({
      jsonrpc: '2.0',
      id: 2,
      method: 'tools/list',
      params: {}
    }) + '\n';
    proc.stdin.write(toolsReq);
  }
});

proc.stderr.on('data', (data) => {
  console.error('[-] MCP stderr:', data.toString());
});

proc.on('close', (code) => {
  console.log(`[*] MCP process exited with code ${code}`);
});

// Send initialize handshake
const initMsg = JSON.stringify({
  jsonrpc: '2.0',
  id: 1,
  method: 'initialize',
  params: {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: {
      name: 'antigravity-client',
      version: '1.0.0'
    }
  }
}) + '\n';

proc.stdin.write(initMsg);

setTimeout(() => {
  console.log('[*] Test complete, closing process.');
  proc.kill();
}, 8000);
