/* Offline behavior checks against the shipped scripts, without a browser or API. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticRoot = path.resolve(__dirname, '../../../services/evidence-viewer/app/static');

class Element {
  constructor() {
    this.hidden = true;
    this.disabled = false;
    this.dataset = {};
    this.value = '';
    this.textContent = '';
    this.scrollTop = 0;
    this.children = [];
    this.listeners = {};
    this.style = { setProperty() {} };
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name),
      toggle: (name, active) => active ? classes.add(name) : classes.delete(name),
    };
  }
  set innerHTML(value) {
    this.html = value;
    this.children = [];
  }
  get innerHTML() { return this.html || ''; }
  querySelectorAll(selector) {
    if (!this.html) return [];
    if (!this.buttons) this.buttons = new Map();
    if (this.buttons.has(this.html + selector)) return this.buttons.get(this.html + selector);
    const result = [];
    for (const match of this.html.matchAll(/<button[^>]*class="([^"]+)"[^>]*data-index="(\d+)"/g)) {
      if (!match[1].split(' ').includes(selector.slice(1))) continue;
      const button = new Element();
      button.dataset.index = match[2];
      result.push(button);
    }
    this.buttons.set(this.html + selector, result);
    return result;
  }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); return item; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  getContext() { return { clearRect() {} }; }
  reset() {}
  load() {}
  pause() {}
  scrollIntoView() {}
  closest(selector) { return this.editable ? this : null; }
}

function loadScript(name) {
  const elements = new Map();
  const document = new Element();
  document.getElementById = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  document.createElement = () => new Element();
  const window = new Element();
  window.location = { hash: '', href: 'http://localhost/' };
  const context = vm.createContext({ document, window, Element, URL, URLSearchParams });
  vm.runInContext(fs.readFileSync(path.join(staticRoot, name), 'utf8'), context);
  const run = code => vm.runInContext(code, context);
  return { run, elements, document };
}

const rows = [
  { camera_id: 'a', camera_name: '入口', event_ts_ms: 1791608400000 },
  { camera_id: 'a', camera_name: '入口', event_ts_ms: 1791607800000 },
  { camera_id: 'b', camera_name: '走廊', event_ts_ms: 1791607200000 },
  { camera_id: 'a', camera_name: '入口', event_ts_ms: 1791606600000 },
];

test('merged route timestamp belongs to the record opened by its button', () => {
  const { run, elements } = loadScript('trajectory.js');
  run(`trajectoryState.rows = ${JSON.stringify(rows)}; renderTrajectoryProfile({personId: '7'});`);
  const stops = run('trajectoryRouteStops(trajectoryState.rows)');
  assert.equal(stops.length, 3, 'a return visit to a camera remains a separate stop');
  assert.equal(stops[2].count, 2);
  assert.equal(stops[2].row.event_ts_ms, rows[stops[2].lastIndex].event_ts_ms);
  const route = elements.get('trajectory-profile');
  route.querySelectorAll('.route-stop').at(-1).listeners.click();
  assert.equal(run('trajectoryState.selectedIndex'), 0);
  assert.ok(route.innerHTML.includes(run(`trajectoryShortDateTime(${rows[0].event_ts_ms})`)));
});

test('profile and route labels describe only the current page on older pages', () => {
  const { run, elements } = loadScript('trajectory.js');
  run(`trajectoryState.rows = ${JSON.stringify(rows)}; trajectoryState.offset = 50;
       renderTrajectoryProfile({personId: '7'});`);
  const html = elements.get('trajectory-profile').innerHTML;
  assert.ok(html.includes('本页路线'));
  assert.ok(html.includes('本页最早'));
  assert.ok(html.includes('本页最新'));
  assert.ok(html.includes('本页最近一次出现'));
  assert.ok(!html.includes('<em>起点</em>'));
});

test('missing trajectory preview falls back once, then shows an actionable empty state', () => {
  const { run, elements } = loadScript('trajectory.js');
  run(`bindTrajectoryEvents();
       renderTrajectoryDetail({camera_name: '入口', full_frame_url: '/gone.jpg', face_crop_url: '/crop.jpg'}, 0);`);
  const image = elements.get('trajectory-detail-image');
  image.listeners.error();
  assert.equal(image.src, '/crop.jpg');
  assert.equal(image.hidden, false);
  image.listeners.error();
  assert.equal(image.hidden, true);
  assert.equal(elements.get('trajectory-detail-empty').hidden, false);
  assert.match(elements.get('trajectory-detail-empty').textContent, /加载失败/);
  run('renderTrajectoryDetail({camera_name: "无图摄像头"}, 1)');
  assert.match(elements.get('trajectory-detail-empty').textContent, /没有可显示/);
});

test('arrow navigation is bounded and does not consume arrows in editable fields', () => {
  const { run, elements, document } = loadScript('trajectory.js');
  run(`trajectoryState.rows = ${JSON.stringify(rows)}; trajectoryDom.view.hidden = false;
       bindTrajectoryEvents(); selectTrajectoryIndex(0);`);
  let prevented = false;
  const input = new Element();
  input.editable = true;
  document.listeners.keydown({ target: input, key: 'ArrowDown', preventDefault() { prevented = true; } });
  assert.equal(run('trajectoryState.selectedIndex'), 0);
  assert.equal(prevented, false);
  document.listeners.keydown({ target: new Element(), key: 'ArrowUp', preventDefault() { prevented = true; } });
  assert.equal(run('trajectoryState.selectedIndex'), 0);
  assert.equal(prevented, true);
  run('selectTrajectoryIndex(999)');
  assert.equal(run('trajectoryState.selectedIndex'), rows.length - 1);
  assert.equal(elements.get('trajectory-older-item').disabled, true);
});

test('reset prevents a slow earlier response from repopulating the trajectory page', async () => {
  const { run, elements } = loadScript('trajectory.js');
  elements.get('trajectory-person-id').value = '7';
  run('let finishRequest; trajectoryRequest = () => new Promise(resolve => { finishRequest = resolve; });');
  const pending = run('searchTrajectory()');
  run('resetTrajectoryPage()');
  run(`finishRequest({trajectory: ${JSON.stringify(rows)}, has_more: true})`);
  await pending;
  assert.equal(run('trajectoryState.rows.length'), 0);
  assert.equal(elements.get('trajectory-search').disabled, false);
  assert.equal(elements.get('trajectory-next').disabled, true);
});

test('evidence selection preserves list scroll and a broken thumbnail has a fallback', () => {
  const { run, elements } = loadScript('evidence.js');
  run(`state.bundles = [{event_id: 'e1', event_type: 'intrusion', face_crop_url: '/gone.jpg'}];`);
  const list = elements.get('bundleList');
  list.scrollTop = 280;
  run('renderBundleList()');
  assert.equal(list.scrollTop, 280);
  const thumb = list.children.at(-1).children[0];
  thumb.children[0].listeners.error();
  assert.ok(thumb.innerHTML.includes('<svg'));
});

test('identity evidence without person ID offers evidence detail, not an unusable trajectory link', () => {
  const { run, elements } = loadScript('evidence.js');
  run(`state.bundles = [{event_id: 'e1', event_type: 'watchlist_hit', clip_status: 'image_ready'}];
       renderBundleList();`);
  const button = elements.get('bundleList').children.at(-1);
  const tags = button.children[1].children[2].children.map(tag => tag.textContent);
  assert.ok(!tags.includes('点击查看此人轨迹'));
  let selected = '';
  run('selectBundle = eventId => { window.selected = eventId; };');
  button.listeners.click();
  selected = run('window.selected');
  assert.equal(selected, 'e1');
  assert.equal(run('state.warnings.size'), 0);
});

test('generated_unverified remains unverified in both evidence list and detail', () => {
  const { run, elements } = loadScript('evidence.js');
  run(`state.bundles = [{event_id: 'e1', event_type: 'intrusion', raw_clip_available: true,
       clip_status: 'generated_unverified', visual_evidence_status: 'unverified'}]; renderBundleList();`);
  const tags = elements.get('bundleList').children.at(-1).children[1].children[2].children.map(tag => tag.textContent);
  assert.deepEqual(tags, ['待复核']);
  run(`state.manifest = {raw_clip_url: '/clip.mov', visual_evidence_status: 'unverified',
       summary: {clip_status: 'generated_unverified'}}; renderDetails();`);
  assert.equal(elements.get('rawClipStatus').textContent, '待复核');
  assert.match(elements.get('clipValidation').textContent, /待复核/);
  assert.equal(elements.get('clipWarning').hidden, false);
});

test('a pending recording and missing image are not described as verified or ready', () => {
  const { run, elements } = loadScript('evidence.js');
  run(`state.manifest = {materialization_status: 'pending', visual_evidence_status: 'unverified', summary: {clip_status: 'pending'}}; renderDetails();`);
  assert.equal(elements.get('clipValidation').textContent, '生成中');
  run(`state.manifest = {playback_kind: 'image', summary: {clip_status: 'image_missing'}}; renderDetails();`);
  assert.equal(elements.get('clipValidation').textContent, '图片暂不可用');
});

test('pending or corrupt evidence cards do not display a verified badge', () => {
  const { run, elements } = loadScript('evidence.js');
  for (const status of ['pending', 'generated_corrupt']) {
    run(`state.bundles = [{event_id: 'e1', event_type: 'intrusion', raw_clip_available: ${status !== 'pending'},
         clip_status: '${status}', visual_evidence_status: 'verified', evidence_state: '${status}'}]; renderBundleList();`);
    const tags = elements.get('bundleList').children.at(-1).children[1].children[2].children.map(tag => tag.textContent);
    assert.ok(!tags.includes('已验证'));
    assert.ok(!tags.includes('generated_corrupt'));
  }
});

test('verified playable video retains its normal detail status', () => {
  const { run, elements } = loadScript('evidence.js');
  run(`state.manifest = {raw_clip_url: '/clip.mov', visual_evidence_status: 'verified',
       summary: {clip_status: 'ready'}}; renderDetails();`);
  assert.equal(elements.get('clipValidation').textContent, '已验证');
  assert.equal(elements.get('clipWarning').hidden, true);
});
