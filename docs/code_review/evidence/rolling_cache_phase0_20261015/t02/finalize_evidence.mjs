import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const base = path.dirname(fileURLToPath(import.meta.url));
const read = name => JSON.parse(fs.readFileSync(path.join(base, name), 'utf8'));
const write = (name, data) => fs.writeFileSync(path.join(base, name), JSON.stringify(data, null, 2) + '\n');
const findings = { generated_at_utc: new Date().toISOString(), runs: {} };
for (const label of ['1013', '1012']) {
  const report = read(label + '_selected_report.json');
  const schema = read(label + '_report_schema.json');
  const analysis = read(label + '_log_analysis.json');
  const config = read(label + '_run_config.json');
  const visibility = report.rolling_cache_segment_visibility;
  const buckets = visibility.metadata_visible_lag_s_by_visible_at_30s;
  findings.runs[label] = {
    source_root: analysis.root,
    report_sha256: analysis.report_sha256,
    formal_window_utc: analysis.formal_window_utc,
    config,
    retention: {
      parameter: config.pressure_rolling_cache_retention_s,
      pressure: report.rolling_cache_sinks_pressure.rolling_cache_retention_seconds,
      observed: Object.fromEntries(Object.entries(report.rolling_cache_sinks_pressure.observed_env).map(([name, env]) => [name, Object.fromEntries(Object.entries(env).filter(([key]) => /RETENTION|CLEANUP|MAINTENANCE|PUBLICATION_WORKERS/.test(key)))])),
    },
    visibility: {
      root: visibility.root,
      source_prefix: visibility.source_prefix,
      segments_measured: visibility.segments_measured,
      metadata_files_scanned: visibility.metadata_files_scanned,
      metadata_visible_lag_s: visibility.metadata_visible_lag_s,
      snapshot_visible_at_buckets_first_utc: buckets[0]?.visible_at_bucket_start,
      snapshot_visible_at_buckets_last_utc: buckets.at(-1)?.visible_at_bucket_start,
      host_override_env_record_missing: analysis.report_host_override_token_count === 0,
      report_root_three_part_evidence_gate: 'not satisfied: historical host override environment was not recorded',
      full_rate_gate_schema: schema.filter(x => /^rolling_cache_full_rate_gate(?:\.|$)/.test(x.path)),
    },
    db_summary_before_cleanup: report.db_summary_before_cleanup,
    cleanup_schema: schema.filter(x => /^pressure_runtime_cleanup_after_restore\.(deleted_|disabled_cameras|removed_evidence_paths|visual_results_retained|removed_rolling_cache\.)/.test(x.path)),
    publication_report_distributions: Object.fromEntries(['rolling_cache_sink_a', 'rolling_cache_sink_b'].map(name => [name, Object.fromEntries(Object.entries(report.diagnostics.log_summary[name]).filter(([key]) => /^rolling_cache_(publication_queue_residence_ms|publication_capacity_wait_ms|publish_total_ms|publish_metadata_fsync_ms|publish_manifest_fsync_ms|publish_staging_dir_fsync_ms|publish_parent_dir_fsync_ms)$/.test(key)))])),
    remux_report: report.diagnostics.log_summary.media_worker.media_remux_ms,
    sample_summary: Object.fromEntries(Object.entries(report.diagnostics.sample_summary).filter(([key, value]) => typeof value !== 'object' && /sample_count|max.*queue|effective_fps/.test(key))),
    analysis_code_evidence: analysis.runtime_samples,
    non_materialized_details: analysis.non_materialized_details ? Object.fromEntries(Object.entries(analysis.non_materialized_details).filter(([key]) => key !== 'task_ids')) : null,
    stop_gate: label === '1013' ? 'T0.3(5): normalized last retry reason and audit detail unavailable in allowed historical artifacts' : null,
  };
}
write('t02_findings.json', findings);

const backup = '/tmp/savant-t02-raw-20261015';
fs.mkdirSync(backup, { recursive: true, mode: 0o700 });
const redactions = [];
for (const name of fs.readdirSync(base)) {
  if (!/\.(json|yml)$/.test(name)) continue;
  const file = path.join(base, name);
  const original = fs.readFileSync(file, 'utf8');
  const sanitized = original.replace(/((?:postgres(?:ql)?|redis|amqp|https?):\/\/)([^/\s"'<>]+)@/g, '$1<redacted>@');
  if (sanitized !== original) {
    fs.copyFileSync(file, path.join(backup, name));
    fs.chmodSync(path.join(backup, name), 0o600);
    fs.writeFileSync(file, sanitized);
    redactions.push({ name, original_sha256: crypto.createHash('sha256').update(original).digest('hex'), sanitized_sha256: crypto.createHash('sha256').update(sanitized).digest('hex') });
  }
}
write('redactions.json', { generated_at_utc: new Date().toISOString(), original_copies_outside_repository: backup, rule: 'URI userinfo redacted; original remote artifact hashes retained separately', files: redactions });
console.log(JSON.stringify({ findings: path.join(base, 't02_findings.json'), redacted_files: redactions.length, backup }));
