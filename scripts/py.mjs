// Run the backend's virtual-environment Python (Windows / macOS / Linux) inside ./backend.
//   node scripts/py.mjs -m app serve
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const backend = join(dirname(fileURLToPath(import.meta.url)), "..", "backend");
const python = process.platform === "win32"
  ? join(backend, ".venv", "Scripts", "python.exe")
  : join(backend, ".venv", "bin", "python");

if (!existsSync(python)) {
  console.error("Python virtual environment not found. Run `npm run setup` first.");
  process.exit(1);
}
const child = spawn(python, process.argv.slice(2), { cwd: backend, stdio: "inherit" });
const stop = () => child.kill("SIGINT");
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
child.on("exit", (code) => process.exit(code ?? 1));
