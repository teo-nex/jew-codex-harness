import pathlib
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "requires Node.js")
class RouterReloadTests(unittest.TestCase):
    def test_reload_requires_owned_idle_service_and_propagates_restart_failure(self):
        script = r'''
import assert from 'node:assert/strict';
import {reloadInstalledRoutes} from './server/reload-router.mjs';
let calls = [];
const healthy = {ok:true, service:'codex-router', activity:{activeCount:0}, resources:{inFlightRequests:0}};
const deps = {assertOwned:()=>calls.push('owned'), readHealth:()=>healthy, restart:()=>calls.push('restart')};
assert.deepEqual(await reloadInstalledRoutes(deps), {reloaded:true});
assert.deepEqual(calls, ['owned','restart']);
for (const health of [{...healthy,activity:{activeCount:1}}, {...healthy,resources:{inFlightRequests:1}},
                      {...healthy,resources:{}}, {...healthy,ok:false}]) {
  calls=[];
  await assert.rejects(reloadInstalledRoutes({...deps,readHealth:()=>health}), /busy|unknown/);
  assert.deepEqual(calls,['owned']);
}
calls=[];
await assert.rejects(reloadInstalledRoutes({...deps,assertOwned:()=>{throw Error('foreign')}}), /foreign/);
assert.deepEqual(calls,[]);
await assert.rejects(reloadInstalledRoutes({...deps,restart:()=>{throw Error('restart failed')}}), /restart failed/);
'''
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                cwd=pathlib.Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
