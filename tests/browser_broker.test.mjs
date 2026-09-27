import assert from 'node:assert/strict';
import test from 'node:test';
import { browserTask } from '../integrations/jev/browser_broker/core.mjs';

const url = 'https://example.com/requested';
const decide = async () => ({ answers: { authorization: { choice: 'allow', confidence: 1 } } });

for (const action of ['read', 'navigate']) {
  for (const [name, pageUrl, loaded, expected] of [
    ['requested page', url, true, 'verified'],
    ['redirected page with the same marker', 'https://example.com/login', true, 'unverified'],
    ['incomplete load', url, false, 'unverified'],
  ]) {
    test(`${action}: ${name}`, async () => {
      const client = { call: async (tool) => {
        if (tool === 'browser_new_tab') return { ok: true, data: { tabId: 'test-tab' } };
        if (tool === 'browser_wait_for') return { ok: loaded, data: {} };
        if (tool === 'browser_read_page') return { ok: true, data: { url: pageUrl, text: 'MARKER' } };
        throw new Error(`Unexpected tool ${tool}`);
      } };
      const result = await browserTask(client, decide, {
        action, goal: 'Read the requested page', url, expected_text: 'MARKER',
      });
      assert.equal(result.status, expected);
    });
  }
}

test('open_link stops on an unexpected initial page before observing or clicking', async () => {
  const called = [];
  const client = { call: async (tool) => {
    called.push(tool);
    if (tool === 'browser_new_tab') return { ok: true, data: { tabId: 'test-tab' } };
    if (tool === 'browser_wait_for') return { ok: true, data: {} };
    if (tool === 'browser_read_page') return { ok: true, data: {
      url: 'https://example.com/unexpected',
      links: [{ text: 'Next', url: 'https://example.com/next' }],
    } };
    throw new Error(`Unexpected tool ${tool}`);
  } };
  const result = await browserTask(client, decide, {
    action: 'open_link', goal: 'Open Next on the requested page', url,
    link_text: 'Next', expected_url: 'https://example.com/next',
  });
  assert.equal(result.status, 'unverified');
  assert.deepEqual(called, ['browser_new_tab', 'browser_wait_for', 'browser_read_page']);
});
