import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const source = process.argv[2];
const target = path.join(path.dirname(new URL(import.meta.url).pathname), process.argv[3] || 'preflight-r1');
const raw = fs.readFileSync(source);
const record = JSON.parse(raw);
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const redact = value => {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [
    key, /password|token|secret|db_url|database_url|redis_url/i.test(key) ? '<redacted>' : redact(item),
  ]));
  if (typeof value === 'string') return value.replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/"']+@/gi, '$1<redacted>@');
  return value;
};
fs.mkdirSync(target, { recursive: true });
const write = (name, value) => fs.writeFileSync(path.join(target, name), JSON.stringify(value, null, 2) + '\n');
write('readonly_validation.sanitized.json', redact(record));
write('snapshot_comparison.json', {
  started: record.started, completed: record.completed,
  container_count: record.container_count, containers_unchanged: record.containers_unchanged,
  snapshot_scope: record.snapshot_scope, stop_reason: record.stop_reason || null,
  before_sha256: hash(JSON.stringify(record.before)), after_sha256: hash(JSON.stringify(record.after)),
});
write('source_sha256.json', record.source_sha256);
const summary = [];
for (const row of record.rounds) {
  write(row.label + '.report.json', redact(row.report));
  fs.writeFileSync(path.join(target, row.label + '.stdout.txt'), row.stdout);
  fs.writeFileSync(path.join(target, row.label + '.stderr.txt'), row.stderr);
  summary.push({ label: row.label, returncode: row.returncode, expected_returncode: row.expected_returncode,
    expectation_met: row.expectation_met, started: row.started, completed: row.completed,
    argv: row.argv, cwd: row.cwd, report_path: row.report_path });
}
write('commands_and_exit_codes.json', summary);
write('provenance.json', { raw_source: source, raw_sha256: hash(raw),
  redaction: 'Only credential-shaped keys and URI userinfo are redacted; stdout/stderr are unchanged.',
  remote_root: summary[0].cwd });
const files = fs.readdirSync(target).filter(name => name !== 'MANIFEST.sha256').sort();
fs.writeFileSync(path.join(target, 'MANIFEST.sha256'), files.map(name => hash(fs.readFileSync(path.join(target, name))) + '  ' + name).join('\n') + '\n');
console.log(JSON.stringify({ container_count: record.container_count, unchanged: record.containers_unchanged,
  rounds: summary.map(({label, returncode, expectation_met}) => ({label, returncode, expectation_met})) }));
