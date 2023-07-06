Contributing
============

Contributions to this repository are welcome from both VUSec members and
external contributors. For any contribution please ensure it is compatible with
the project's license.

VUSec members have push access and can either directly push their code to the
main branch, or submit a PR if a review is required (e.g., significant changes
to existing features). External collaborators can open an issue or PR.

Please keep in mind this repository is a generic framework, used by many
different projects as a submodule. Therefore, contributions should be generic
and useful to other projects as well. Project-specific packages, instances and
targets can be easily plugged into the framework from your own project's
config. If your projects requires infra patches because the functionality is
not present in the infra, please consider creating a generic patch for the
infra that will also be useful for other projects.

This infra is used by many different people and projects for gathering data for
their research and publications.
**Therefore, correctness of the results produced by this infra is critical.**
Try to make default behavior correct/obvious, or tell the user explicitly
something may be wrong or ask for explicit configuration.

Code style
==========

All Python code in this repository is auto-formatted using `black` and `isort`,
although this is not enforced automatically. Therefore, before pushing, run the
following commands, or configure your IDE to run the equivalent::

    black --preview .
    isort .

The `setup.cfg` file configures `isort` to follow Black's code style.
Both of these tools can be obtained via Python's package manager
(e.g., `pip3 install black isort`).

In addition, users can use `flake8` to perform additional linting.
The `setup.cfg` sets up `flake8` to follow this code style.

All code in this repository is compatible with static type checking
using both `pyright` and `mypy`. When contributing new code, please ensure it
passes type checking in these tools. They can be run from the root of the
repository using::

    mypy .
    pyright infra/

Most editors can be configured to automatically run these tools.

Documentation is generated using sphinx3, mostly based on Python docstrings.
This documentation is automatically generated for the git main branch.
To build the documentation locally, run the following command::

    make -C docs/

Testing and compatibility
=========================

All users should add the infra as a git submodule pinned to a specific version
(i.e., commit). In this way, any breaking changes in the infra won't affect
your project unless you manually update the submodule. Having said that, do try
to preserve backwards compatibility with the existing interfaces so projects
can keep up-to-date with infra improvements easily.

Sadly the infra does not currently contain any automated tests. It is
recommended to run a number of commands and targets manually to test if your
changes break any existing code. If you are unsure if your patch might affect
correctness, please ask for a review in a PR.
