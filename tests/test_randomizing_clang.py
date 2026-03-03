import pytest
from unittest.mock import MagicMock

from infra.context import Context
from infra.instances.clang import ParameterizedClang, RandomizingClang
from infra.util import FatalError


class TestParameterizedClang:
    def test_extra_flags_applied(self, ctx: Context, mock_llvm: MagicMock) -> None:
        inst = ParameterizedClang(
            mock_llvm,
            "test",
            extra_cflags=["-Dfoo"],
            extra_cxxflags=["-Dbar"],
            extra_ldflags=["-lbaz"],
            extra_lib_ldflags=["-lqux"],
        )
        inst.configure(ctx)
        assert "-Dfoo" in ctx.cflags
        assert "-Dbar" in ctx.cxxflags
        assert "-lbaz" in ctx.ldflags
        assert "-lqux" in ctx.lib_ldflags

class TestRandomizingClang:
    def test_seed_substitution_in_flags(
        self, ctx: Context, mock_llvm: MagicMock
    ) -> None:
        inst = RandomizingClang(
            mock_llvm,
            "rando",
            extra_cflags=["-seed=RNGSEED"],
            extra_ldflags=["-Wl,--rng=RNGSEED"],
        )
        ctx.rngseed = "12345"
        inst.configure(ctx)
        assert "-seed=12345" in ctx.cflags
        assert "-Wl,--rng=12345" in ctx.ldflags

    def test_missing_seed_raises_error(
        self, ctx: Context, mock_llvm: MagicMock
    ) -> None:
        inst = RandomizingClang(mock_llvm, "rando")
        assert ctx.rngseed == ""
        with pytest.raises(FatalError, match="requires an RNG seed"):
            inst.configure(ctx)

    def test_multiple_placeholders_in_same_flag(
        self, ctx: Context, mock_llvm: MagicMock
    ) -> None:
        inst = RandomizingClang(
            mock_llvm,
            "rando",
            extra_cflags=["-a=RNGSEED -b=RNGSEED"],
        )
        ctx.rngseed = "99"
        inst.configure(ctx)
        assert "-a=99 -b=99" in ctx.cflags
