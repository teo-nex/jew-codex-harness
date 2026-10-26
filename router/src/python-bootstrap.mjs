// The pinned LiteLLM release supports CPython >=3.10,<3.15. A generic Windows
// `py -3` can choose a newer, incompatible interpreter despite a supported
// `python` already being on PATH. Select an existing compatible executable;
// this helper never installs Python or changes the active environment.
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const PROBE = "import encodings, json, sys; print(json.dumps({'executable': sys.executable, 'version': list(sys.version_info[:2])}))";

export function compatiblePython(command, args = [], { spawn = spawnSync } = {}) {
  try {
    const result = spawn(command, [...args, "-I", "-c", PROBE], {
      encoding: "utf8", timeout: 15_000, windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (result.error || result.status !== 0) return undefined;
    const value = JSON.parse(result.stdout);
    const version = value?.version;
    if (Array.isArray(version) && version[0] === 3 && Number.isInteger(version[1])
        && version[1] >= 10 && version[1] < 15
        && typeof value.executable === "string" && value.executable.trim()) {
      return value.executable;
    }
  } catch { /* Missing launchers and invalid probes are not usable runtimes. */ }
  return undefined;
}

export function selectBootstrapPython({ platform = process.platform, spawn = spawnSync } = {}) {
  const versions = ["3.12", "3.13", "3.11", "3.14", "3.10"];
  const candidates = platform === "win32"
    ? [["python", []], ...versions.map(version => ["py", [`-${version}`]])]
    : [["python3", []], ...versions.map(version => [`python${version}`, []])];
  for (const [command, args] of candidates) {
    const executable = compatiblePython(command, args, { spawn });
    if (executable) return executable;
  }
  throw new Error("LiteLLM requires an existing Python 3.10–3.14 or uv (managed Python 3.12).");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv[2] === "check") {
    process.exit(compatiblePython(process.argv[3]) ? 0 : 1);
  }
  try { console.log(selectBootstrapPython()); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
