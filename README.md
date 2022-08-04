About
=====

[![Documentation Status](https://readthedocs.org/projects/instrumentation-infra/badge/?version=master)](https://instrumentation-infra.readthedocs.io/en/master/?badge=master)

*instrumentation-infra* is an infrastructure for program instrumentation. It
builds benchmark programs with custom instrumentation flags (e.g., LLVM passes)
and runs them. Documentation is available [here][docs].

For an example of how to this infra, see the [skeleton repository][skeleton].
See our [infra-sanitizers][sanitizers] repository for a large collection of
sanitizers that have already been ported to use this infrastructure.

[docs]: http://instrumentation-infra.readthedocs.io
[skeleton]: https://github.com/vusec/instrumentation-skeleton
[sanitizers]: https://github.com/vusec/infra-sanitizers
