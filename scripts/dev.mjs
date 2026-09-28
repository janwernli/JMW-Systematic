// Start backend (FastAPI :8000) and frontend (Vite :5173) together; Ctrl+C stops both.
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const win = process.platform === "win32";
const procs = [
  ["api", "\x1b[34m", process.execPath, [join(root, "scripts", "py.mjs"), "-m", "app", "serve"]],
  ["web", "\x1b[32m", "npm", ["--prefix", "frontend", "run", "dev"]],
];
const children = procs.map(([name, color, cmd, args]) => {
  const env = { ...process.env, FORCE_COLOR: "1" };
  const c = cmd === "npm"
    ? spawn(`npm ${args.join(" ")}`, { cwd: root, shell: true, env })
    : spawn(cmd, args, { cwd: root, env });
  const pipe = (stream, out) => stream.on("data", (d) => {
    for (const line of d.toString().split(/\r?\n/)) if (line.trim()) out.write(`${color}[${name}]\x1b[0m ${line}\n`);
  });
  pipe(c.stdout, process.stdout);
  pipe(c.stderr, process.stderr);
  c.on("exit", (code) => {
    console.log(`[${name}] exited with code ${code}`);
    shutdown();
  });
  return c;
});
console.log("Backend  http://127.0.0.1:8765/api/health   Frontend  http://127.0.0.1:5173  (first start seeds demo data, ~20-30 s)");
let stopping = false;
function shutdown() {
  if (stopping) return;
  stopping = true;
  for (const c of children) {
    if (win && c.pid) spawn("taskkill", ["/pid", String(c.pid), "/T", "/F"], { stdio: "ignore" });
    else c.kill("SIGINT");
  }
  setTimeout(() => process.exit(0), 500);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
