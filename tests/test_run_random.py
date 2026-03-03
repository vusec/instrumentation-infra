import argparse
from unittest.mock import MagicMock, patch

import pytest

from infra.commands.run_random import RunRandomCommand
from infra.context import Context
from infra.util import Index


@pytest.fixture
def targets_index(mock_target: MagicMock) -> Index:
    idx: Index = Index("target")
    idx[mock_target.name] = mock_target
    return idx


@pytest.fixture
def instances_index(mock_instance: MagicMock) -> Index:
    idx: Index = Index("instance")
    idx[mock_instance.name] = mock_instance
    return idx


class TestRunRandomCommand:
    @patch("infra.commands.run_random.BuildCommand")
    def test_each_iteration_gets_unique_seed_and_original_ctx_is_untouched(
        self,
        MockBuildCommand: MagicMock,
        ctx: Context,
        mock_target: MagicMock,
        mock_instance: MagicMock,
        targets_index: Index,
        instances_index: Index,
    ) -> None:
        cmd = RunRandomCommand()
        cmd.instances = instances_index
        cmd.targets = targets_index
        cmd.packages = Index("package")
        cmd.make_pool = MagicMock(return_value=MagicMock())

        ctx.args = argparse.Namespace(
            target="test-target",
            instances=["test-instance"],
            iterations=3,
        )

        MockBuildCommand.return_value = MagicMock()

        # Snapshot original state
        original_rngseed = ctx.rngseed
        original_uniqueid = ctx.uniqueid
        original_cflags = list(ctx.cflags)

        cmd.run(ctx)

        # Each iteration should have produced a run with a distinct seed
        run_seeds = [
            c[0][0].rngseed for c in mock_target.run.call_args_list
        ]
        assert len(run_seeds) == 3
        assert len(set(run_seeds)) == 3, f"expected 3 unique seeds, got {run_seeds}"

        # The caller's context must be untouched
        assert ctx.rngseed == original_rngseed
        assert ctx.uniqueid == original_uniqueid
        assert ctx.cflags == original_cflags

    @patch("infra.commands.run_random.BuildCommand")
    def test_flags_do_not_accumulate_across_iterations(
        self,
        MockBuildCommand: MagicMock,
        ctx: Context,
        mock_target: MagicMock,
        mock_instance: MagicMock,
        targets_index: Index,
        instances_index: Index,
    ) -> None:
        """Verify that configure() starts from a clean slate each iteration."""
        cmd = RunRandomCommand()
        cmd.instances = instances_index
        cmd.targets = targets_index
        cmd.packages = Index("package")
        cmd.make_pool = MagicMock(return_value=MagicMock())

        ctx.args = argparse.Namespace(
            target="test-target",
            instances=["test-instance"],
            iterations=3,
        )

        MockBuildCommand.return_value = MagicMock()

        # Make configure add a flag so we can detect accumulation
        def add_flag(c: Context) -> None:
            c.cflags.append("-Xtest")

        mock_instance.configure = MagicMock(side_effect=add_flag)

        cmd.run(ctx)

        # Each run context should have exactly 1 -Xtest; if flags
        # accumulated the later iterations would have 2 or 3.
        for c in mock_target.run.call_args_list:
            run_ctx = c[0][0]
            assert run_ctx.cflags.count("-Xtest") == 1
