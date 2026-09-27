#!/usr/bin/env node
// TypeSafe's current edge rejects Python urllib (Cloudflare 1010) but accepts
// the same bounded request through Node fetch. Secrets arrive only on stdin.
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
let input;
try { input = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
catch { process.stderr.write('invalid input\n'); process.exit(2); }
if (!input || typeof input.key !== 'string' || !input.key || typeof input.body !== 'object' || !input.body) {
  process.stderr.write('invalid request\n'); process.exit(2);
}
const timeout = Math.min(10000, Math.max(1000, Number(input.timeout_ms) || 4000));
const endpoints = {
  typesafe: 'https://api.typesafe.ai/v1/systemone',
  openrouter: 'https://openrouter.ai/api/alpha/decisions',
};
const provider = input.provider || 'typesafe';
const endpoint = Object.hasOwn(endpoints, provider) ? endpoints[provider] : undefined;
if (!endpoint) { process.stderr.write('unsupported Jev provider\n'); process.exit(2); }
try {
  const response = await fetch(endpoint, {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(timeout),
    headers: { authorization: 'Bearer ' + input.key, 'content-type': 'application/json' },
    body: JSON.stringify(input.body),
  });
  if (!response.ok) {
    process.stderr.write('provider HTTP ' + response.status + '\n');
    process.exit(1);
  }
  const value = await response.json();
  if (!value || typeof value !== 'object' || !value.answers || typeof value.answers !== 'object')
    throw new Error('invalid provider response');
  process.stdout.write(JSON.stringify(value));
} catch (error) {
  process.stderr.write(error?.name === 'TimeoutError' ? 'provider timeout\n' : 'provider unavailable\n');
  process.exit(1);
}
