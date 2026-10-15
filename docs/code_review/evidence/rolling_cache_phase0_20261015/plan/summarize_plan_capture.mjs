import fs from 'node:fs';
import crypto from 'node:crypto';

const [source, output] = process.argv.slice(2);
const c = JSON.parse(JSON.parse(fs.readFileSync(source, 'utf8')).stdout);
const live = JSON.parse(c.commands.inspect.stdout);
const images = JSON.parse(c.commands.image_inspect.stdout);
const envMap = rows => Object.fromEntries((rows || []).map(row => {
  const at = row.indexOf('=');
  return [row.slice(0, at), row.slice(at + 1)];
}));
const hash = value => crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const safeValue = (key, value) => /^(ROLLING_CACHE|MEDIA_WORKER|RUNTIME_EPOCH|FRAME_CACHE_SIDECAR|EVIDENCE_DENSITY_PROFILE)/.test(key)
  ? (key === 'ROLLING_CACHE_SOURCES' ? { count: value ? value.split(',').length : 0, first: value?.split(',')[0], last: value?.split(',').at(-1) } : value)
  : { present: value !== undefined, sha256: hash(value ?? null) };
const result = {
  captured_at_utc: c.utc,
  source,
  compose_version: c.commands.compose_version.stdout.trim(),
  clean_environment: { PATH: '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', HOME: '/home/user', USER: 'user', LANG: 'C', TZ: 'Asia/Seoul' },
  ambient_matching_key_names: c.commands.ambient_key_names.keys,
  files: Object.fromEntries(Object.entries(c.files).map(([k, v]) => [k, { exists: v.exists, sha256: v.sha256, bytes: v.bytes }])),
  config_results: {},
  service_diffs: {},
  api: {},
};
for (const variant of ['base_clean', 'storage_operator_clean']) {
  const r = c.commands[variant];
  result.config_results[variant] = { args: r.args, returncode: r.returncode, stderr: r.stderr, project_name: r.project_name, all_service_count: r.all_services?.length };
  if (!r.services) continue;
  result.service_diffs[variant] = {};
  for (const [name, rendered] of Object.entries(r.services)) {
    const i = live.find(row => row.Config.Labels['com.docker.compose.service'] === name);
    const image = images.find(row => row.Id === i.Image);
    const actual = envMap(i.Config.Env);
    const declared = Object.fromEntries(Object.entries(rendered.environment || {}).filter(([, v]) => v !== null).map(([k, v]) => [k, String(v)]));
    const effective = { ...envMap(image.Config.Env), ...declared };
    const keys = [...new Set([...Object.keys(actual), ...Object.keys(effective)])].sort();
    const changes = keys.filter(k => actual[k] !== effective[k]);
    const before = i.Mounts.map(m => ({ type: m.Type, source: m.Source, target: m.Destination, read_only: !m.RW, propagation: m.Propagation }));
    const after = rendered.volumes.map(m => ({ type: m.type, source: m.source, target: m.target, read_only: !!m.read_only }));
    result.service_diffs[variant][name] = {
      image_id: i.Image,
      rendered_image: rendered.image,
      rendered_tag_points_to_current_image: (image.RepoTags || []).includes(rendered.image),
      live_cpuset_cpus: i.HostConfig.CpusetCpus,
      rendered_cpuset_cpus: rendered.cpuset || '',
      live_config_files: i.Config.Labels['com.docker.compose.project.config_files'],
      before_mounts: before,
      rendered_mounts: after,
      effective_env_difference_count: changes.length,
      effective_env_differences: changes.map(k => ({ key: k, live: safeValue(k, actual[k]), rendered_with_same_image_defaults: safeValue(k, effective[k]) })),
    };
  }
}
const t = c.api['topology-config'].data.data;
result.api.topology = { saved_config: t.saved_config, plan: t.plan, preflight: t.preflight };
result.api.control = { note: 'mode is hard-coded to single in runtime_control_status; inspect individual states', data: c.api.control.data.data };
result.api.apply_status = c.api['topology-config/apply-status'].data.data;
fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n');
console.log(JSON.stringify({ output, utc: result.captured_at_utc, services: Object.fromEntries(Object.entries(result.service_diffs.storage_operator_clean).map(([k, v]) => [k, { effective_env_differences: v.effective_env_difference_count, cpuset: [v.live_cpuset_cpus, v.rendered_cpuset_cpus], image_tag_matches: v.rendered_tag_points_to_current_image }])) }));
