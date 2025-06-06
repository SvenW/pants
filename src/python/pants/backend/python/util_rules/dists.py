# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import io
import os
from collections import abc
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import toml

from pants.backend.python.subsystems import setuptools
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.subsystems.setuptools import Setuptools
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.pex import Pex, PexRequest, VenvPex, VenvPexProcess
from pants.backend.python.util_rules.pex import rules as pex_rules
from pants.backend.python.util_rules.pex_requirements import EntireLockfile, PexRequirements
from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.engine.fs import (
    CreateDigest,
    Digest,
    DigestContents,
    DigestSubset,
    FileContent,
    MergeDigests,
    PathGlobs,
    RemovePrefix,
    Snapshot,
)
from pants.engine.internals.selectors import Get
from pants.engine.process import ProcessResult
from pants.engine.rules import collect_rules, rule
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.osutil import is_macos_big_sur
from pants.util.strutil import ensure_text, softwrap


class BuildBackendError(Exception):
    pass


class InvalidBuildConfigError(Exception):
    pass


@dataclass(frozen=True)
class BuildSystemRequest:
    """A request to find build system config in the given dir of the given digest."""

    digest: Digest
    working_directory: str


@dataclass(frozen=True)
class BuildSystem:
    """A PEP 517/518 build system configuration."""

    requires: PexRequirements | EntireLockfile
    build_backend: str

    @classmethod
    def legacy(cls, _setuptools: Setuptools) -> BuildSystem:
        return cls(_setuptools.pex_requirements(), "setuptools.build_meta:__legacy__")


@rule
async def find_build_system(request: BuildSystemRequest, _setuptools: Setuptools) -> BuildSystem:
    digest_contents = await Get(
        DigestContents,
        DigestSubset(
            request.digest,
            PathGlobs(
                globs=[os.path.join(request.working_directory, "pyproject.toml")],
                glob_match_error_behavior=GlobMatchErrorBehavior.ignore,
            ),
        ),
    )
    ret = None
    if digest_contents:
        file_content = next(iter(digest_contents))
        settings: Mapping[str, Any] = toml.loads(file_content.content.decode())
        build_system = settings.get("build-system")
        if build_system is not None:
            build_backend = build_system.get("build-backend")
            if build_backend is None:
                raise InvalidBuildConfigError(
                    f"No build-backend found in the [build-system] table in {file_content.path}"
                )
            requires = build_system.get("requires")
            if requires is None:
                raise InvalidBuildConfigError(
                    f"No requires found in the [build-system] table in {file_content.path}"
                )
            ret = BuildSystem(PexRequirements(requires), build_backend)
    # Per PEP 517: "If the pyproject.toml file is absent, or the build-backend key is missing,
    #   the source tree is not using this specification, and tools should revert to the legacy
    #   behaviour of running setup.py."
    if ret is None:
        ret = BuildSystem.legacy(_setuptools)
    return ret


@dataclass(frozen=True)
class DistBuildRequest:
    """A request to build dists via a PEP 517 build backend."""

    build_system: BuildSystem

    # TODO: Support backend_path (https://www.python.org/dev/peps/pep-0517/#in-tree-build-backends)

    interpreter_constraints: InterpreterConstraints
    build_wheel: bool
    build_sdist: bool
    input: Digest
    working_directory: str  # Relpath within the input digest.
    dist_source_root: str  # Source root of the python_distribution target
    build_time_source_roots: tuple[str, ...]  # Source roots for 1st party build-time deps.
    output_path: str  # Location of the output directory within dist dir.

    target_address_spec: str | None = None  # Only needed for logging etc.
    wheel_config_settings: FrozenDict[str, tuple[str, ...]] | None = None
    sdist_config_settings: FrozenDict[str, tuple[str, ...]] | None = None

    extra_build_time_requirements: tuple[Pex, ...] = tuple()
    extra_build_time_env: Mapping[str, str] | None = None


@dataclass(frozen=True)
class DistBuildResult:
    output: Digest
    # Relpaths in the output digest.
    wheel_path: str | None
    sdist_path: str | None


# Note that the shim is capable of building a wheel and an sdist in one invocation, and that
# is how we currently run it.  We may in the future choose to invoke it twice, for finer-grained
# invalidation (e.g., so that we don't rebuild an sdist on wheel_config_settings changes).
# But then we would incur two process executions instead of one, and it's not yet clear if that is
# preferable. Even if we do decide to run in two passes, it'll still be better to have a single
# shim for both, so we can use the same merged digest, instead of needing two almost-identical
# ones, that differ only on the shim.
_BACKEND_SHIM_BOILERPLATE = """
# DO NOT EDIT THIS FILE -- AUTOGENERATED BY PANTS

import errno
import os
import {build_backend_module}

backend = {build_backend_object}

dist_dir = "{dist_dir}"
build_wheel = {build_wheel}
build_sdist = {build_sdist}
wheel_config_settings = {wheel_config_settings_str}
sdist_config_settings = {sdist_config_settings_str}

# Python 2.7 doesn't have the exist_ok arg on os.makedirs().
try:
    os.makedirs(dist_dir)
except OSError as e:
    if e.errno != errno.EEXIST:
        raise

wheel_path = backend.build_wheel(dist_dir, wheel_config_settings) if build_wheel else None
sdist_path = backend.build_sdist(dist_dir, sdist_config_settings) if build_sdist else None

if wheel_path:
    print("wheel: {{wheel_path}}".format(wheel_path=wheel_path))
if sdist_path:
    print("sdist: {{sdist_path}}".format(sdist_path=sdist_path))
"""


def interpolate_backend_shim(dist_dir: str, request: DistBuildRequest) -> bytes:
    # See https://www.python.org/dev/peps/pep-0517/#source-trees.
    module_path, _, object_path = request.build_system.build_backend.partition(":")
    backend_object = f"{module_path}.{object_path}" if object_path else module_path

    def config_settings_repr(cs: FrozenDict[str, tuple[str, ...]] | None) -> str:
        # setuptools.build_meta expects list values and chokes on tuples.
        # We assume/hope that other backends accept lists as well.
        return distutils_repr(None if cs is None else {k: list(v) for k, v in cs.items()})

    return _BACKEND_SHIM_BOILERPLATE.format(
        build_backend_module=module_path,
        build_backend_object=backend_object,
        dist_dir=dist_dir,
        build_wheel=request.build_wheel,
        build_sdist=request.build_sdist,
        wheel_config_settings_str=config_settings_repr(request.wheel_config_settings),
        sdist_config_settings_str=config_settings_repr(request.sdist_config_settings),
    ).encode()


@rule
async def run_pep517_build(request: DistBuildRequest, python_setup: PythonSetup) -> DistBuildResult:
    # Note that this pex has no entrypoint. We use it to run our generated shim, which
    # in turn imports from and invokes the build backend.
    build_backend_pex = await Get(
        VenvPex,
        PexRequest(
            output_filename="build_backend.pex",
            internal_only=True,
            requirements=request.build_system.requires,
            pex_path=request.extra_build_time_requirements,
            interpreter_constraints=request.interpreter_constraints,
        ),
    )

    # This is the setuptools dist directory, not Pants's, so we hardcode to dist/.
    dist_dir = "dist"
    backend_shim_name = "backend_shim.py"
    backend_shim_path = os.path.join(request.working_directory, backend_shim_name)
    backend_shim_digest = await Get(
        Digest,
        CreateDigest(
            [
                FileContent(
                    backend_shim_path,
                    interpolate_backend_shim(os.path.join(dist_dir, request.output_path), request),
                ),
            ]
        ),
    )

    merged_digest = await Get(Digest, MergeDigests((request.input, backend_shim_digest)))

    extra_env = {
        **(request.extra_build_time_env or {}),
        "PEX_EXTRA_SYS_PATH": os.pathsep.join(request.build_time_source_roots),
    }
    if python_setup.macos_big_sur_compatibility and is_macos_big_sur():
        extra_env["MACOSX_DEPLOYMENT_TARGET"] = "10.16"

    result = await Get(
        ProcessResult,
        VenvPexProcess(
            build_backend_pex,
            argv=(backend_shim_name,),
            input_digest=merged_digest,
            extra_env=extra_env,
            working_directory=request.working_directory,
            output_directories=(dist_dir,),  # Relative to the working_directory.
            description=(
                f"Run {request.build_system.build_backend} for {request.target_address_spec}"
                if request.target_address_spec
                else f"Run {request.build_system.build_backend}"
            ),
            level=LogLevel.DEBUG,
        ),
    )
    output_lines = result.stdout.decode().splitlines()
    paths = {}
    for line in output_lines:
        for dist_type in ["wheel", "sdist"]:
            if line.startswith(f"{dist_type}: "):
                paths[dist_type] = os.path.join(
                    request.output_path, line[len(dist_type) + 2 :].strip()
                )
    # Note that output_digest paths are relative to the working_directory.
    output_digest = await Get(Digest, RemovePrefix(result.output_digest, dist_dir))
    output_snapshot = await Get(Snapshot, Digest, output_digest)
    for dist_type, path in paths.items():
        if path not in output_snapshot.files:
            raise BuildBackendError(
                softwrap(
                    f"""
                    Build backend {request.build_system.build_backend} did not create
                    expected {dist_type} file {path}
                    """
                )
            )
    return DistBuildResult(
        output_digest, wheel_path=paths.get("wheel"), sdist_path=paths.get("sdist")
    )


# Distutils does not support unicode strings in setup.py, so we must explicitly convert to binary
# strings as pants uses unicode_literals. A natural and prior technique was to use `pprint.pformat`,
# but that embeds u's in the string itself during conversion. For that reason we roll out own
# literal pretty-printer here.
#
# Note that we must still keep this code, even though Pants only runs with Python 3, because
# the created product may still be run by Python 2.
#
# For more information, see http://bugs.python.org/issue13943.
def distutils_repr(obj) -> str:
    """Compute a string repr suitable for use in generated setup.py files."""
    output = io.StringIO()
    linesep = os.linesep

    def _write(data):
        output.write(ensure_text(data))

    def _write_repr(o, indent=False, level=0):
        pad = " " * 4 * level
        if indent:
            _write(pad)
        level += 1

        if isinstance(o, (bytes, str)):
            # The py2 repr of str (unicode) is `u'...'` and we don't want the `u` prefix; likewise,
            # the py3 repr of bytes is `b'...'` and we don't want the `b` prefix so we hand-roll a
            # repr here.
            o_txt = ensure_text(o)
            if linesep in o_txt:
                _write('"""{}"""'.format(o_txt.replace('"""', r"\"\"\"")))
            else:
                _write("'{}'".format(o_txt.replace("'", r"\'")))
        elif isinstance(o, abc.Mapping):
            _write("{" + linesep)
            for k, v in o.items():
                _write_repr(k, indent=True, level=level)
                _write(": ")
                _write_repr(v, indent=False, level=level)
                _write("," + linesep)
            _write(pad + "}")
        elif isinstance(o, abc.Iterable):
            if isinstance(o, abc.MutableSequence):
                open_collection, close_collection = "[]"
            elif isinstance(o, abc.Set):
                open_collection, close_collection = "{}"
            else:
                open_collection, close_collection = "()"

            _write(open_collection + linesep)
            for i in o:
                _write_repr(i, indent=True, level=level)
                _write("," + linesep)
            _write(pad + close_collection)
        else:
            _write(repr(o))  # Numbers and bools.

    _write_repr(obj)
    return output.getvalue()


def rules():
    return (*collect_rules(), *setuptools.rules(), *pex_rules())
