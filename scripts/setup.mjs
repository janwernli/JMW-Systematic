// One-time setup: Python venv + backend deps, frontend deps. Works on Windows, macOS and Linux.
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const backend = join(root, "backend");
const win = process.platform === "win32";
const venvPy = win ? join(backend, ".venv", "Scripts", "python.exe") : join(backend, ".venv", "bin", "python");

function run(cmd, args, cwd = root) {
  console.log(`\n> ${cmd} ${args.join(" ")}`);
  const r = spawnSync(cmd, args, { cwd, stdio: "inherit", shell: win && cmd === "npm" });
  if (r.status !== 0) {
    console.error(`\nCommand failed: ${cmd} ${args.join(" ")}`);
    process.exit(r.status ?? 1);
  }
}

function findPython() {
  const candidates = win ? [["py", ["-3.12"]], ["py", ["-3"]], ["python", []]] : [["python3.12", []], ["python3", []], ["python", []]];
  for (const [cmd, pre] of candidates) {
    const r = spawnSync(cmd, [...pre, "-c", "import sys; assert sys.version_info >= (3, 11); print(sys.version)"], { encoding: "utf8" });
    if (r.status === 0) return [cmd, pre];
  }
  console.error("Python 3.11+ not found. Install Python 3.12 from https://www.python.org/downloads/ and re-run `npm run setup`.");
  process.exit(1);
}

if (!existsSync(venvPy)) {
  const [py, pre] = findPython();
  run(py, [...pre, "-m", "venv", ".venv"], backend);
}
run(venvPy, ["-m", "pip", "install", "--upgrade", "pip"], backend);
run(venvPy, ["-m", "pip", "install", "-r", "requirements-dev.txt"], backend);
run("npm", ["install", "--no-audit", "--no-fund"], join(root, "frontend"));
if (!existsSync(join(root, ".env"))) {
  copyFileSync(join(root, ".env.example"), join(root, ".env"));
  console.log("\nCreated .env from .env.example (demo mode).");
}
console.log("\nSetup complete. Start the app with:  npm run dev   then open http://127.0.0.1:5173");
