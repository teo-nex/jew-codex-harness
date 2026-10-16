#!/usr/bin/env node
// TypeSafe's current edge rejects Python urllib (Cloudflare 1010) but accepts
// the same bounded request through Node fetch. Secrets arrive only on stdin.
const fail = (stage, reason, http_status, exitCode = 1) => {
  process.stderr.write(JSON.stringify({ stage, reason, ...(Number.isInteger(http_status) ? { http_status } : {}) }) + '\n');
  process.exit(exitCode);
};
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
    fail('transport', 'http_error', response.status);
  }
  let value;
  try { value = await response.json(); }
  catch (error) {
    fail(error instanceof SyntaxError ? 'response_parse' : 'transport',
      error instanceof SyntaxError ? 'invalid_json' : error?.name === 'TimeoutError' ? 'timeout' : 'connection_failed');
  }
  if (!value || typeof value !== 'object' || Array.isArray(value))
    fail('response_validation', 'invalid_response_type');
  if (!value.answers || typeof value.answers !== 'object' || Array.isArray(value.answers))
    fail('response_validation', 'missing_answers');
  process.stdout.write(JSON.stringify(value));
} catch (error) {
  fail('transport', error?.name === 'TimeoutError' ? 'timeout' : 'connection_failed');
}
