import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const root = process.cwd();
const target = path.dirname(new URL(import.meta.url).pathname);
const hash = data => crypto.createHash('sha256').update(data).digest('hex');
const inputs = {
  'baseline-summary.json': '/tmp/pressure60-baseline-summary.json',
  'current-summary.json': '/tmp/pressure60-final-summary.json',
  'dependencies.txt': '/tmp/pressure60-harness-lock.txt',
  'baseline-junit.xml': '/tmp/pressure60-baseline-stable.xml',
  'current-junit.xml': '/tmp/pressure60-final.xml',
  'new-tests-junit.xml': '/tmp/pressure60-final-new-tests.xml',
  'new-tests-pytest.log': '/tmp/pressure60-final-new-tests.log',
  'new-test-pollution-single.json': '/private/var/folders/tm/p5cbz9yd4xn7wjtmdl9167vr0000gn/T/pytest-of-khonsu/pytest-90/test_collector_reads_mode_sink0/baseline_pollution.json',
  'new-test-pollution-dual.json': '/private/var/folders/tm/p5cbz9yd4xn7wjtmdl9167vr0000gn/T/pytest-of-khonsu/pytest-90/test_collector_reads_mode_sink1/baseline_pollution.json',
  'baseline-pytest.log': '/tmp/pressure60-baseline-stable.log',
  'current-pytest.log': '/tmp/pressure60-final-pytest.log',
  'minimum-results.json': '/tmp/pressure60-readonly-validation/results.json',
  'doctor-summary.json': '/tmp/pressure60-readonly-validation/doctor-summary.json',
  'deployment-contract.log': '/tmp/pressure60-readonly-validation/deployment-contract.stdout',
  'smoke.log': '/tmp/pressure60-readonly-validation/smoke.stdout',
  'readonly-docker-wrapper.py': '/tmp/pressure60-readonly-validation/docker',
};
for (const [name, source] of Object.entries(inputs)) {
  fs.copyFileSync(source, path.join(target, name));
}
const baseline = JSON.parse(fs.readFileSync(inputs['baseline-summary.json']));
const current = JSON.parse(fs.readFileSync(inputs['current-summary.json']));
const identities = result => result.failures_and_errors.map(item => item.nodeid + ':' + item.kind);
const oldFailures = new Set(identities(baseline));
const newFailures = new Set(identities(current));
const sourcePaths = [
  'scripts/runtime/run_midterm_pressure60.py',
  'scripts/runtime/pressure_storage_guard.py',
  'scripts/runtime/rolling_cache_storage_preflight.py',
  'scripts/runtime/pressure_runtime_readback.py',
  'scripts/runtime/pressure_baseline_guard.py',
  ...fs.readdirSync(path.join(root, 'harness/tests')).filter(name => /^test_midterm_pressure60.*\.py$/.test(name)).map(name => 'harness/tests/' + name),
].sort();
const summary = {
  status: 'STOP',
  stop_reason: 'Final full harness has six failures absent from the baseline; the taskbook stage-1 stop condition is met.',
  current_harness_returncode: 1,
  generated_at_kst: new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'medium',
  }).format(new Date()) + '+09:00',
  baseline_counts: baseline.counts,
  current_counts: current.counts,
  added_test_cases: 164,
  new_tests_selection: 'Exact (classname, name) set difference between current-junit.xml and baseline-junit.xml, run as pytest nodeids with DATABASE_URL and VIDEO_ANALYTICS_DATABASE_URL removed.',
  new_tests_counts: { tests: 164, failures: 2, errors: 0, skipped: 0, passed: 162 },
  new_tests_returncode: 1,
  added_failures_or_errors: [...newFailures].filter(item => !oldFailures.has(item)),
  removed_failures_or_errors: [...oldFailures].filter(item => !newFailures.has(item)),
  comparison_passed: [...newFailures].every(item => oldFailures.has(item)),
  source_sha256: Object.fromEntries(sourcePaths.map(name => [name, hash(fs.readFileSync(path.join(root, name)))])),
  exact_base_config: {
    command: 'docker compose -f infra/docker-compose.midterm.yml config',
    returncode: 0,
    started_at_kst_lower_bound: '2026-10-15T21:06:06+09:00',
    stdout_sha256: hash(fs.readFileSync('/tmp/pressure60-readonly-validation/compose-base-exact.stdout')),
    note: 'Run through the same readonly SSH wrapper. Full rendering is retained outside Git; it is not a service operation.',
  },
  notes: [
    'Source hashes identify the exact tested tree; current-summary code_sha is a precommit label.',
    'Raw suite timestamps retain the local UTC+08 offset; displayed audit times use KST.',
    'Minimum-results source hash predates only subsequent runner fixes; its compose/config/doctor inputs are unchanged.',
    'Current full harness has 17 failures, including six not present in the baseline; the previous 11-failure result does not describe this tested tree.',
    'No repair or test rerun follows this stop; new-tests-junit.xml was already produced before the stop decision.',
  ],
};
fs.writeFileSync(path.join(target, 'comparison.json'), JSON.stringify(summary, null, 2) + '\n');
const files = fs.readdirSync(target).filter(name => name !== 'MANIFEST.sha256' && fs.statSync(path.join(target, name)).isFile()).sort();
fs.writeFileSync(path.join(target, 'MANIFEST.sha256'), files.map(name => hash(fs.readFileSync(path.join(target, name))) + '  ' + name).join('\n') + '\n');
console.log(JSON.stringify({ counts: current.counts, comparison_passed: summary.comparison_passed, files: files.length }));
