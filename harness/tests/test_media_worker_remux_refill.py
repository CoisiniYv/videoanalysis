"""A skipped candidate must not waste a free remux slot or break source fairness."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from test_evidence_source_fairness import fairness_db  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def import_state():
    original = list(sys.path)
    for name in list(sys.modules):
        if name == 'app' or name.startswith('app.'):
            del sys.modules[name]
    sys.path.insert(0, str(ROOT / 'services' / 'media-worker'))
    yield
    sys.path[:] = original
    for name in list(sys.modules):
        if name == 'app' or name.startswith('app.'):
            del sys.modules[name]


def cfg(**overrides):
    values = dict(
        rolling_cache_sources=(),
        rolling_cache_materialization_max_per_poll=1,
        rolling_cache_materialization_candidate_lookahead=4,
        rolling_cache_materialization_aging_seconds=60.0,
        rolling_cache_materialization_processing_deadline_seconds=300.0,
        rolling_cache_root='/unused/rolling',
        rolling_cache_materialized_root='/unused/materialized',
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class Conn:
    def __init__(self):
        self.sql = ''
        self.params = {}

    def cursor(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return []


@pytest.fixture
def refill(monkeypatch):
    from app import worker

    release = Event()
    runtime = worker.MaterializationResources(
        database_url='postgresql://unused', max_active=4,
        remux_workers=2, source_limit=1,
    )
    state = SimpleNamespace(
        rows=[{'event_id': str(i), 'source_id': f'camera-{i}'} for i in range(10)],
        prepared=[], served=[], queries=[], turns=[], blocked=None,
        skip='none', reject_all=False,
    )

    def turns(_conn, **kwargs):
        state.turns.append(kwargs)
        return [SimpleNamespace(source_id=row['source_id'])
                for row in state.rows[:kwargs['limit']]]

    def candidates(_conn, _cfg, **kwargs):
        state.queries.append(kwargs)
        rows = [row for row in state.rows if row['source_id'] in kwargs['turn_sources']]
        if state.skip == 'source_busy' and state.blocked is None:
            # A competing lane acquires the source after source selection.
            state.blocked = runtime.source_slots.try_acquire('camera-0')
        return rows[:kwargs['limit']]

    def prepare(_conn, _cfg, row, **_kwargs):
        state.prepared.append(row['event_id'])
        if state.reject_all or row['event_id'] == '0':
            if state.skip == 'exception':
                raise RuntimeError('prepare failed')
            if state.skip != 'source_busy':
                return None
        return {'event_id': row['event_id'], 'source_id': row['source_id']}

    def materialize(**_kwargs):
        assert release.wait(5)
        return {}

    monkeypatch.setattr(worker, 'next_source_turns', turns)
    monkeypatch.setattr(worker, '_rolling_cache_candidate_tasks', candidates)
    monkeypatch.setattr(worker, '_prepare_rolling_cache_job', prepare)
    monkeypatch.setattr(worker, '_materialize_rolling_cache_job', materialize)
    monkeypatch.setattr(worker, '_defer_rolling_cache_task', lambda *_a, **_k: None)
    monkeypatch.setattr(worker, 'mark_served', lambda _c, **kw: state.served.append(kw['source_id']))
    runner = worker._RollingCacheMaterializationRunner(max_workers=1, runtime_resources=runtime)
    runner._rotation_enabled = True
    try:
        yield runner, runtime, state
    finally:
        release.set()
        runner.force_stop(Conn())
        runtime.close(wait=True)
        if state.blocked is not None:
            state.blocked.release()


@pytest.mark.parametrize('reason', ['none', 'exception', 'source_busy'])
def test_skipped_first_candidate_refills_in_the_same_poll(refill, reason):
    runner, runtime, state = refill
    state.skip = reason
    runner.process(Conn(), cfg())

    assert [flight[0] for flight in runner._futures.values()] == ['1']
    assert state.served == ['camera-1']
    assert len(state.queries) == 1
    assert state.queries[0]['limit'] == 5
    assert state.turns[0]['limit'] == 5
    assert runtime.work_budget.snapshot()['active'] == 1
    assert runtime.remux_lane.snapshot()['reserved'] == 1


def test_rejecting_every_candidate_is_bounded_and_releases_all_capacity(refill):
    runner, runtime, state = refill
    state.reject_all = True
    runner.process(Conn(), cfg())

    assert state.prepared == ['0', '1', '2', '3', '4']
    assert len(state.queries) == 1
    assert not runner._futures and not state.served
    assert runtime.work_budget.snapshot()['active'] == 0
    assert runtime.remux_lane.snapshot()['reserved'] == 0
    assert runtime.source_slots.saturated_sources() == ()


def test_lookahead_zero_restores_a_single_candidate_window(refill):
    runner, runtime, state = refill
    runner.process(Conn(), cfg(rolling_cache_materialization_candidate_lookahead=0))
    assert state.prepared == ['0']
    assert state.queries[0]['limit'] == 1
    assert not runner._futures
    assert runtime.work_budget.snapshot()['active'] == 0


def test_lookahead_does_not_increase_claim_limit_or_scan_when_lane_is_full(refill):
    runner, runtime, state = refill
    runner.max_workers = 2
    runner.process(Conn(), cfg())
    assert len(runner._futures) == 1  # max_per_poll remains one
    assert runtime.work_budget.snapshot()['active'] == 1
    runner.max_workers = 1
    count = len(state.queries)
    runner.process(Conn(), cfg())
    assert len(state.queries) == count


@pytest.mark.parametrize('lookahead,expected', [('-1', 0), ('4', 4), ('999', 64)])
def test_refill_config_bounds_lookahead(monkeypatch, lookahead, expected):
    from app.config import load_config
    monkeypatch.setenv('ROLLING_CACHE_MATERIALIZATION_CANDIDATE_LOOKAHEAD', lookahead)
    assert load_config().rolling_cache_materialization_candidate_lookahead == expected


@pytest.mark.parametrize('age,expected', [('-1', 0.0), ('0', 0.0), ('60', 60.0)])
def test_aging_config_can_be_disabled(monkeypatch, age, expected):
    from app.config import load_config
    monkeypatch.setenv('ROLLING_CACHE_MATERIALIZATION_AGING_SECONDS', age)
    assert load_config().rolling_cache_materialization_aging_seconds == expected


def test_candidate_query_keeps_rotation_before_age_across_sources():
    from app import worker
    conn = Conn()
    worker._rolling_cache_candidate_tasks(
        conn, cfg(), limit=5, per_source_limit=1,
        turn_sources=('camera-new', 'camera-old'),
    )
    assert conn.params['aging_seconds'] == 60.0
    order = conn.sql.rsplit('ORDER BY', 1)[1]
    assert order.index('priority DESC') < order.index('source_rank ASC')
    assert order.index('source_rank ASC') < order.index('array_position')
    assert order.index('array_position') < order.index('aged_ready_at ASC')
    within_source = conn.sql.split('ROW_NUMBER() OVER', 1)[1].split('AS source_rank', 1)[0]
    assert within_source.index('aged_ready_at ASC') < within_source.index('materialization_due_at ASC')
    # Retry eligibility is still a WHERE predicate, even for very old tasks.
    assert 'et.materialization_next_attempt_at' in conn.sql
    assert ') <= now()' in conn.sql


def test_pressure_snapshot_preserves_refill_settings(monkeypatch):
    path = ROOT / 'scripts' / 'runtime' / 'run_midterm_pressure60.py'
    spec = importlib.util.spec_from_file_location('pressure_refill', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    values = {
        'ROLLING_CACHE_MATERIALIZATION_CANDIDATE_LOOKAHEAD': '0',
        'ROLLING_CACHE_MATERIALIZATION_AGING_SECONDS': '15',
    }
    monkeypatch.setattr(module, 'docker_container_env', lambda _name: values)
    snapshot = module.media_worker_rolling_cache_env_snapshot()
    assert {key: snapshot[key] for key in values} == values


@pytest.mark.parametrize('age_seconds,expected_old', [(60.0, True), (0.0, False)])
def test_real_postgres_aged_retry_keeps_its_place_within_source(
    fairness_db, age_seconds, expected_old,
):
    from app import worker
    conn, add = fairness_db
    add('camera-A', 1, age_s=120)
    old = conn.execute('SELECT event_id FROM evidence_tasks').fetchone()[0]
    conn.execute("UPDATE evidence_tasks SET materialization_next_attempt_at = now() - interval '1 second'")
    add('camera-A', 1, age_s=10)
    rows = worker._rolling_cache_candidate_tasks(
        conn, cfg(rolling_cache_materialization_aging_seconds=age_seconds),
        limit=1, per_source_limit=1, turn_sources=('camera-A',),
    )
    assert (rows[0]['event_id'] == old) is expected_old


def test_real_postgres_old_source_cannot_steal_another_sources_turn(fairness_db):
    from app import worker
    conn, add = fairness_db
    add('camera-old', 20, age_s=120)
    add('camera-new', 1, age_s=10)
    rows = worker._rolling_cache_candidate_tasks(
        conn, cfg(), limit=5, per_source_limit=4,
        turn_sources=('camera-new', 'camera-old'),
    )
    assert rows[0]['source_id'] == 'camera-new'
    assert rows[1]['source_id'] == 'camera-old'
    assert rows[0]['source_rank'] == rows[1]['source_rank'] == 1


def test_real_postgres_aging_preserves_priority_and_retry_due_gates(fairness_db):
    from app import worker
    conn, add = fairness_db
    add('camera-old', 1, age_s=120)
    add('camera-priority', 1, priority=100, age_s=1)
    rows = worker._rolling_cache_candidate_tasks(
        conn, cfg(), limit=5, per_source_limit=1,
        turn_sources=('camera-old', 'camera-priority'),
    )
    assert rows[0]['source_id'] == 'camera-priority'
    conn.execute("UPDATE evidence_tasks SET materialization_next_attempt_at = now() + interval '1 hour' WHERE source_id = 'camera-old'")
    rows = worker._rolling_cache_candidate_tasks(conn, cfg(), limit=5, per_source_limit=1)
    assert [row['source_id'] for row in rows] == ['camera-priority']
