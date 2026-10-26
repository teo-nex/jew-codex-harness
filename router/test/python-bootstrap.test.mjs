import assert from "node:assert/strict";
import test from "node:test";
import { compatiblePython, selectBootstrapPython } from "../src/python-bootstrap.mjs";

const probe = (minor, executable = "fixture-python") => ({
  status: 0, stdout: JSON.stringify({ executable, version: [3, minor] }),
});

test("a supported PATH Python is preferred over the Windows launcher default", () => {
  const calls = [];
  const selected = selectBootstrapPython({ platform: "win32", spawn(command, args) {
    calls.push([command, args]);
    return command === "python" ? probe(11, "fixture-3.11") : probe(15);
  } });
  assert.equal(selected, "fixture-3.11");
  assert.equal(calls.length, 1);
  assert.ok(calls[0][1].includes("-I"));
});

test("an incompatible default falls back to an explicitly supported launcher version", () => {
  const calls = [];
  const selected = selectBootstrapPython({ platform: "win32", spawn(command, args) {
    calls.push([command, args[0]]);
    return command === "python" ? probe(15) : probe(12, "fixture-3.12");
  } });
  assert.equal(selected, "fixture-3.12");
  assert.deepEqual(calls, [["python", "-I"], ["py", "-3.12"]]);
});

test("incompatible or damaged Python installations fail closed", () => {
  for (const result of [probe(9), probe(15), { status: 0, stdout: "invalid" },
                        { status: 1 }, { status: null, error: new Error("missing") }]) {
    assert.equal(compatiblePython("fixture", [], { spawn: () => result }), undefined);
  }
  assert.throws(() => selectBootstrapPython({ spawn: () => probe(15) }), /Python 3.10–3.14/);
});

test("POSIX selection can use an installed versioned Python", () => {
  assert.equal(selectBootstrapPython({ platform: "linux", spawn(command) {
    return command === "python3.12" ? probe(12, "/fixture/python3.12") : probe(15);
  } }), "/fixture/python3.12");
});
