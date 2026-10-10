"""Decoder-output identity checks without requiring a GPU or GStreamer."""
from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[2] / 'modules/savant_security/savant_patches/v0.6.0/decoded_frame_guard.py'


@pytest.fixture
def guard_type():
    spec = importlib.util.spec_from_file_location('decoded_frame_guard_under_test', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DecodedFrameGuard


class Pipeline:
    def __init__(self):
        self.frames = {}
        self.moves = []

    def add(self, index, source='camera', stage='decode'):
        self.frames[index] = (SimpleNamespace(source_id=source, uuid=f'uuid-{index}', pts=index), stage)

    def get_independent_frame(self, index):
        if index not in self.frames:
            raise ValueError('Frame not found')
        frame, stage = self.frames[index]
        if stage == 'batch':
            raise ValueError('Frame is already batched')
        return frame, None

    def move_as_is(self, destination, indices):
        for index in indices:
            frame, stage = self.frames[index]
            if stage not in ('decode', 'source-convert'):
                raise ValueError('Stage source-convert is out of order. Source stage is muxer')
            self.moves.append(index)
            self.frames[index] = frame, destination


def make_guard(guard_type, *, history_size=8):
    pipeline = Pipeline()
    dropped = []
    guard = guard_type(pipeline, on_drop=lambda **row: dropped.append(row), history_size=history_size)
    return guard, pipeline, dropped


def test_unique_frames_advance_once_and_keep_identity(guard_type):
    guard, pipeline, dropped = make_guard(guard_type)
    pipeline.add(10)
    assert guard.admit(10, source_id='camera', buffer_pts=10)
    assert pipeline.frames[10][0].uuid == 'uuid-10'
    assert pipeline.moves == [10]
    assert dropped == []


def test_duplicate_is_rejected_even_before_first_output_leaves_convert(guard_type):
    guard, pipeline, dropped = make_guard(guard_type)
    pipeline.add(10)
    assert guard.admit(10, source_id='camera', buffer_pts=10)
    assert not guard.admit(10, source_id='camera', buffer_pts=11)
    assert pipeline.moves == [10]
    assert dropped[0]['reason'] == 'duplicate'
    assert dropped[0]['frame_id'] == 10


def test_out_of_order_unseen_identity_is_dropped_without_deleting_original(guard_type):
    guard, pipeline, dropped = make_guard(guard_type)
    pipeline.add(10, stage='muxer')
    assert not guard.admit(10, source_id='camera', buffer_pts=10)
    assert pipeline.frames[10][1] == 'muxer'
    assert dropped[0]['reason'] == 'stage_rejected'
    pipeline.add(11)
    assert guard.admit(11, source_id='camera', buffer_pts=11)


def test_evicted_history_cannot_reintroduce_backward_stage_panic(guard_type):
    guard, pipeline, dropped = make_guard(guard_type, history_size=2)
    for index in (10,11,12):
        pipeline.add(index)
        assert guard.admit(index, source_id='camera', buffer_pts=index)
        frame, _ = pipeline.frames[index]
        pipeline.frames[index] = frame, 'muxer'
    assert len(guard.recent_ids) == 2
    assert not guard.admit(10, source_id='camera', buffer_pts=10)
    assert dropped[-1]['reason'] == 'stage_rejected'


def test_unique_reordered_ids_are_allowed(guard_type):
    guard, pipeline, dropped = make_guard(guard_type)
    for index in (10,7,9):
        pipeline.add(index)
        assert guard.admit(index, source_id='camera', buffer_pts=index)
    assert pipeline.moves == [10,7,9]
    assert not dropped


def test_concurrent_duplicate_has_one_winner(guard_type):
    guard, pipeline, dropped = make_guard(guard_type)
    pipeline.add(10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:guard.admit(10,source_id='camera',buffer_pts=10), range(2)))
    assert sorted(results) == [False,True]
    assert pipeline.moves == [10]
    assert len(dropped) == 1


@pytest.mark.parametrize('index', [None,-1,'not-an-id'])
def test_invalid_metadata_does_not_enter_pipeline(guard_type,index):
    guard, pipeline, dropped = make_guard(guard_type)
    assert not guard.admit(index, source_id='camera', buffer_pts=10)
    assert not pipeline.moves
    assert dropped[0]['reason'] == 'missing_metadata'


@pytest.mark.parametrize('stage', ['missing','batch'])
def test_deleted_and_batched_frames_fail_closed(guard_type,stage):
    guard, pipeline, dropped = make_guard(guard_type)
    if stage != 'missing':
        pipeline.add(10,stage=stage)
    assert not guard.admit(10,source_id='camera',buffer_pts=10)
    assert not pipeline.moves
    assert dropped[0]['reason'] == 'frame_unavailable'


def test_mismatched_source_does_not_reassign_identity(guard_type):
    guard, pipeline, dropped = make_guard(guard_type)
    pipeline.add(10,source='different-camera')
    assert not guard.admit(10,source_id='camera',buffer_pts=10)
    assert not pipeline.moves
    assert dropped[0]['reason'] == 'source_mismatch'
    assert pipeline.frames[10][0].source_id == 'different-camera'


def test_drop_observer_failure_does_not_admit_bad_buffer(guard_type):
    pipeline = Pipeline()
    def broken(**row):
        raise RuntimeError('metrics/log observer unavailable')
    guard = guard_type(pipeline,on_drop=broken,history_size=2)
    assert not guard.admit(99,source_id='camera',buffer_pts=10)
