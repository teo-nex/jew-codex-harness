/** Probe the router's Responses transform without printing stream contents. */
import { Readable } from 'node:stream';
import { createResponsesStreamTransform } from '../router/src/openai-adapters.mjs';

try {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 8 * 1024 * 1024) throw new Error('oversize');
    chunks.push(chunk);
  }
  const input = Buffer.concat(chunks);
  const added_indices = [];
  let terminal_output_types = [];
  for (const frame of input.toString('utf8').split(/\r?\n\r?\n/)) {
    const data = frame.split(/\r?\n/).find(line => line.startsWith('data:'));
    if (!data || added_indices.length >= 15) continue;
    try {
      const value = JSON.parse(data.slice(5).trimStart());
      if (value?.type === 'response.output_item.added') {
        added_indices.push({ index: value.output_index ?? null,
          item_index: value.item?.output_index ?? null,
          item_type: String(value.item?.type || 'unknown').slice(0, 32) });
      }
      if (value?.type === 'response.completed' && Array.isArray(value.response?.output)) {
        terminal_output_types = value.response.output.slice(0, 15).map(item =>
          String(item?.type || 'unknown').slice(0, 32));
      }
    } catch {}
  }
  const output = [];
  const stream = Readable.from([input]).pipe(createResponsesStreamTransform());
  for await (const chunk of stream) output.push(chunk);
  const text = Buffer.concat(output).toString('utf8');
  const issues = [];
  const reasons = [];
  const known = new Map([
    ['The Responses stream emitted data after its terminal event.', 'post_terminal_event'],
    ['The Responses stream changed response IDs.', 'changed_response_id'],
    ['The Responses completion used a different response ID.', 'completion_id_mismatch'],
    ['A Responses output item used a non-sequential output index.', 'nonsequential_output_index'],
    ['A Responses output item used conflicting output indices.', 'conflicting_output_index'],
    ['A Responses output item reused an ID with a different index.', 'reused_item_id'],
    ['A Responses function call arguments event referenced an unknown item.', 'unknown_function_item'],
    ['A Responses function call arguments event used the wrong output index.', 'wrong_function_output_index'],
    ['A Responses function call arguments event requires call_id or item_id.', 'missing_function_id'],
  ]);
  for (const frame of text.split(/\r?\n\r?\n/)) {
    const data = frame.split(/\r?\n/).find(line => line.startsWith('data:'));
    if (!data) continue;
    try {
      const value = JSON.parse(data.slice(5).trimStart());
      if (value?.type === 'error' || value?.type === 'response.failed') {
        issues.push(String(value.code || value.response?.error?.code || 'unknown').slice(0, 64));
        reasons.push(known.get(value.message) || 'other');
      }
    } catch {}
  }
  process.stdout.write(JSON.stringify({ ok: issues.length === 0, issue_codes: issues.slice(0, 3), issue_reasons: reasons.slice(0, 3), added_indices, terminal_output_types }) + '\n');
} catch {
  process.stdout.write(JSON.stringify({ ok: false, issue_codes: ['transform_exception'], issue_reasons: ['other'] }) + '\n');
}
