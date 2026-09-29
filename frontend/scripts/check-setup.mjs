import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const frontendDir = fileURLToPath(new URL('../', import.meta.url));

// No third-party imports: this must work even before npm ci has been run.
export function checkSetup(directory = frontendDir, nodeVersion = process.versions.node) {
  const [major, minor] = nodeVersion.split('.').map(Number);
  if (major < 22 || (major === 22 && minor < 12)) {
    return [`Node ${nodeVersion} is unsupported. Use Node 22.12+ (with nvm: nvm install && nvm use).`];
  }

  const readJson = (path) => JSON.parse(readFileSync(resolve(directory, path), 'utf8'));
  const manifest = readJson('package.json');
  const lock = readJson('package-lock.json');
  const problems = [];
  for (const name of Object.keys({ ...manifest.dependencies, ...manifest.devDependencies })) {
    const expected = lock.packages[`node_modules/${name}`]?.version;
    if (!expected) {
      problems.push(`${name}: missing from package-lock.json; update and commit the frontend lockfile.`);
      continue;
    }
    try {
      const installed = readJson(`node_modules/${name}/package.json`).version;
      if (installed !== expected) {
        problems.push(`${name}: installed ${installed}, lockfile requires ${expected}.`);
      }
    } catch {
      problems.push(`${name}: missing or unreadable in frontend/node_modules.`);
    }
  }
  return problems;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const problems = checkSetup();
    if (problems.length) {
      console.error(`Frontend setup needs attention:\n- ${problems.join('\n- ')}\n\nFrom frontend/, run npm ci, then npm run dev.`);
      process.exitCode = 1;
    }
  } catch (error) {
    console.error(`Cannot check frontend setup: ${error.message}\nRestore frontend/package.json and package-lock.json, then run npm ci from frontend/.`);
    process.exitCode = 1;
  }
}
