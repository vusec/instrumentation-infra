import logging
import os
from unittest.mock import MagicMock

import pytest

from infra.context import Context, ContextPaths


@pytest.fixture
def tmp_paths(tmp_path: str) -> ContextPaths:
    """Create a ContextPaths pointing at temporary directories."""
    setup_path = os.path.join(str(tmp_path), "setup.py")
    # Create the setup.py file so root is tmp_path
    with open(setup_path, "w") as f:
        f.write("")

    paths = ContextPaths(
        infra=os.path.join(str(tmp_path), "infra"),
        setup=setup_path,
        workdir=str(tmp_path),
    )

    # Create directories that the infra expects to exist
    os.makedirs(paths.log, exist_ok=True)
    os.makedirs(paths.packages, exist_ok=True)
    os.makedirs(paths.targets, exist_ok=True)
    os.makedirs(paths.pool_results, exist_ok=True)

    return paths


@pytest.fixture
def ctx(tmp_paths: ContextPaths) -> Context:
    """Create a minimal Context for testing."""
    logger = logging.getLogger("test")
    logger.addHandler(logging.NullHandler())
    return Context(paths=tmp_paths, log=logger)


@pytest.fixture
def mock_llvm() -> MagicMock:
    llvm = MagicMock()
    llvm.configure = MagicMock()
    return llvm


@pytest.fixture
def mock_target() -> MagicMock:
    target = MagicMock()
    target.name = "test-target"
    target.add_run_args = MagicMock()
    target.add_build_args = MagicMock()
    target.goto_rootdir = MagicMock()
    target.run_hooks_pre_run = MagicMock()
    target.run = MagicMock()
    target.run_hooks_post_run = MagicMock()
    return target


@pytest.fixture
def mock_instance() -> MagicMock:
    instance = MagicMock()
    instance.name = "test-instance"
    instance.add_build_args = MagicMock()
    instance.add_run_args = MagicMock()
    instance.configure = MagicMock()
    instance.prepare_run = MagicMock()
    instance.process_run = MagicMock()
    return instance
