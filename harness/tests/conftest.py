"""pytest configuration for video-analytics harness tests."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--gpu",
        action="store_true",
        default=False,
        help="Run GPU-dependent smoke tests",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "gpu: GPU-dependent test (requires --gpu flag)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--gpu"):
        return  # Don't skip GPU tests when --gpu is passed
    skip_gpu = pytest.mark.skip(reason="requires --gpu option")
    for item in items:
        if "gpu" in item.keywords:
            item.add_marker(skip_gpu)
