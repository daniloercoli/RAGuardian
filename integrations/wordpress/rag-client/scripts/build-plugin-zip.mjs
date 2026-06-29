import {execFileSync} from "node:child_process";
import {cpSync, existsSync, mkdirSync, rmSync} from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";

const scriptsDir = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(scriptsDir, "..");
const distDir = path.join(pluginRoot, "dist");
const stagingRoot = path.join(distDir, "staging");
const stagingPlugin = path.join(stagingRoot, "rag-client");
const zipPath = path.join(distDir, "rag-client.zip");

const entries = [
  "assets",
  "includes",
  "README.md",
  "rag-client.php",
];

rmSync(stagingRoot, {recursive: true, force: true});
rmSync(zipPath, {force: true});
mkdirSync(stagingPlugin, {recursive: true});

for (const entry of entries) {
  const source = path.join(pluginRoot, entry);
  if (!existsSync(source)) {
    throw new Error(`Missing required plugin entry: ${entry}`);
  }
  cpSync(source, path.join(stagingPlugin, entry), {recursive: true});
}

try {
  execFileSync("zip", ["-qr", zipPath, "rag-client"], {
    cwd: stagingRoot,
    stdio: "inherit",
  });
} catch (error) {
  throw new Error("Cannot create plugin zip. Install the `zip` command and retry.");
} finally {
  rmSync(stagingRoot, {recursive: true, force: true});
}

console.log(`Created ${zipPath}`);
