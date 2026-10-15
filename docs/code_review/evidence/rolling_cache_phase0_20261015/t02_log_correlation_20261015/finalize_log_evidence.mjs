import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const base = path.dirname(fileURLToPath(import.meta.url));
const backup = '/tmp/savant-t02-log-correlation-raw-20261015';
fs.mkdirSync(backup, { recursive: true, mode: 0o700 });
const changes = [];
for (const name of fs.readdirSync(base)) {
  if (!name.endsWith('.json') || name === 'redactions.json') continue;
  const file = path.join(base, name);
  const original = fs.readFileSync(file, 'utf8');
  const sanitized = original.replace(/((?:postgres(?:ql)?|redis|amqp|https?):\/\/)([^/\s"'<>]+)@/g, '$1<redacted>@');
  if (sanitized === original) continue;
  fs.copyFileSync(file, path.join(backup, name));
  fs.chmodSync(path.join(backup, name), 0o600);
  fs.writeFileSync(file, sanitized);
  changes.push({ name, original_sha256: crypto.createHash('sha256').update(original).digest('hex'), sanitized_sha256: crypto.createHash('sha256').update(sanitized).digest('hex') });
}
fs.writeFileSync(path.join(base, 'redactions.json'), JSON.stringify({ generated_at_utc: new Date().toISOString(), original_copies_outside_repository: backup, rule: 'URI userinfo redacted; remote input SHA256 retained in summary.inputs', files: changes }, null, 2) + '\n');
const entries = fs.readdirSync(base).filter(name => name !== 'MANIFEST.sha256' && fs.statSync(path.join(base, name)).isFile()).sort();
fs.writeFileSync(path.join(base, 'MANIFEST.sha256'), entries.map(name => crypto.createHash('sha256').update(fs.readFileSync(path.join(base, name))).digest('hex') + '  ' + name).join('\n') + '\n');
console.log(JSON.stringify({ files: entries.length, redacted_files: changes.length, backup }));
