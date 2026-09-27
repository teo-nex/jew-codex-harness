#!/usr/bin/env node
/** One compact, Jev-reviewed CanvasTTY Browser Use MCP tool. */
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';
import { browserTask } from './core.mjs';

const codexHome = process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
const helper = process.env.CANVASTTY_MCP_HELPER || '/Applications/CanvasTTY.app/Contents/Resources/agent-browser/mcp-helper.mjs';
const { GatewayClient, readIdentity } = await import(pathToFileURL(helper).href);
const { localAsk } = await import(pathToFileURL(path.join(codexHome, 'jev-global/ui.mjs')).href);

const IDENTITY_KEYS = [
  'CANVASTTY_AGENT_BROWSER_ADDRESS', 'CANVASTTY_AGENT_ID',
  'CANVASTTY_AGENT_CONNECTION_ID', 'CANVASTTY_TERMINAL_SESSION_ID',
  'CANVASTTY_AGENT_PROVIDER', 'CANVASTTY_AGENT_CAPABILITY'
];
const TOOL = {
  name: 'browser_task',
  description: 'Run a bounded CanvasTTY browser task. Internally reads the page, asks Jev before actions, verifies the result, and returns only a compact summary. Use this instead of raw browser tools for link and page tasks.',
  inputSchema: {
    type: 'object', additionalProperties: false,
    properties: {
      action: { type: 'string', enum: ['read', 'open_link', 'navigate'] },
      goal: { type: 'string', minLength: 1, maxLength: 600 },
      url: { type: 'string', minLength: 1, maxLength: 2048 },
      link_text: { type: 'string', maxLength: 120 },
      expected_url: { type: 'string', maxLength: 2048 },
      expected_text: { type: 'string', maxLength: 160 },
      fallback_navigate: { type: 'boolean' }
    },
    required: ['action', 'goal', 'url']
  }
};

let client = null;
let ready = false;
try {
  const identity = readIdentity();
  for (const key of IDENTITY_KEYS) delete process.env[key];
  client = new GatewayClient(identity);
} catch {
  for (const key of IDENTITY_KEYS) delete process.env[key];
}

const reply = (id, result) => ({ jsonrpc: '2.0', id: id ?? null, result });
const failure = (id, code, message) => ({ jsonrpc: '2.0', id: id ?? null,
  error: { code, message } });

async function handle(message) {
  if (!message || message.jsonrpc !== '2.0') return failure(message?.id, -32600, 'Invalid Request');
  if (message.method === 'notifications/initialized') return null;
  if (message.method === 'ping') return reply(message.id, {});
  if (message.method === 'initialize') {
    if (client) {
      try { await client.connect(); ready = true; }
      catch { ready = false; }
    }
    return reply(message.id, { protocolVersion: '2025-06-18',
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: 'jev-browser', version: '0.1.0' } });
  }
  if (message.method === 'tools/list') return reply(message.id, { tools: ready ? [TOOL] : [] });
  if (message.method === 'tools/call') {
    if (message.params?.name !== 'browser_task' || !ready)
      return failure(message.id, -32601, 'Browser tool unavailable');
    try {
      const value = await browserTask(client,
        ({ state, questions }) => localAsk(state, questions), message.params.arguments ?? {});
      const bad = !['verified', 'verified_with_navigation'].includes(value.status);
      return reply(message.id, { content: [{ type: 'text', text: JSON.stringify(value) }], isError: bad });
    } catch (error) {
      return reply(message.id, { content: [{ type: 'text', text: JSON.stringify({
        status: 'error', reason: String(error?.message || 'browser_unavailable').slice(0, 160)
      }) }], isError: true });
    }
  }
  return failure(message.id, -32601, 'Method not found');
}

let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  buffer += chunk;
  if (buffer.length > 524288) { buffer = ''; return; }
  let newline;
  while ((newline = buffer.indexOf('\n')) !== -1) {
    const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
    let message;
    try { message = JSON.parse(line); }
    catch { process.stdout.write(JSON.stringify(failure(null, -32700, 'Parse error')) + '\n'); continue; }
    void handle(message).then(result => { if (result) process.stdout.write(JSON.stringify(result) + '\n'); });
  }
});
process.stdin.on('end', () => client?.close());
