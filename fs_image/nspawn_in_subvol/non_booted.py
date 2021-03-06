#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

'''
Read the `run.py` docblock first.  Then, review the docs for
`new_nspawn_opts` and `PopenArgs`, and invoke `popen_non_booted_nspawn`.

This file uses `systemd-nspawn --as-pid2` to run nspawn's internal "stub
init" as PID 1 of the container, and have that start `opts.cmd` as PID 2.

Security note: We use `--console=pipe`, which means that FDs that point at
your terminal may make it inside the container, allowing the guest to
synthesize keystrokes on the host.
'''
import functools
import subprocess

from contextlib import contextmanager
from typing import Iterable

from .args import _NspawnOpts, PopenArgs
from .cmd import maybe_popen_and_inject_fds, _NspawnSetup, _nspawn_setup
from .common import _nspawn_version, _PopenWrapper


def run_non_booted_nspawn(
    opts: _NspawnOpts, popen_args: PopenArgs,
    *, popen_wrappers: Iterable[_PopenWrapper] = (),  # Doc on `_PopenWrapper`
) -> subprocess.CompletedProcess:
    with functools.reduce(
        (lambda x, f: f(x)), popen_wrappers, popen_non_booted_nspawn
    )(opts, popen_args) as proc:
        cmd_stdout, cmd_stderr = proc.communicate()
    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout=cmd_stdout,
        stderr=cmd_stderr,
    )


@contextmanager
def popen_non_booted_nspawn(
    opts: _NspawnOpts, popen_args: PopenArgs,
) -> Iterable[subprocess.Popen]:
    with _nspawn_setup(opts, popen_args) as setup, \
            _popen_non_booted_nspawn(setup) as proc:
        yield proc


@contextmanager
def _popen_non_booted_nspawn(setup: _NspawnSetup) -> Iterable[subprocess.Popen]:
    opts = setup.opts
    # Lets get the version locally right up front.  If this fails we'd like to
    # know early rather than later.
    version = _nspawn_version()

    cmd = [
        *setup.nspawn_cmd,
        # Add `--as-pid2` to run an nspawn-provided stub "init" process as
        # PID 1 of the container, which starts our actual workload as PID 2.
        # The command in `opts.cmd` is not (currently) meant to be an "init"
        # process.  And a PID 1 of a PID namespace must be a functioning
        # "init" at least insofar as signal handling is concerned.
        '--as-pid2',
        f'--user={opts.user.pw_name}',
    ]
    # This is last to let the user have final say over the environment.
    cmd.extend(['--setenv=' + se for se in setup.cmd_env])
    if version >= 242:
        # This essentially reverts to pre-242 behavior, where the container
        # has direct access to the caller's FDs 0/1/2, which may be the
        # running host's TTY/PTY (or other privileged FDs).  This is a
        # SECURITY RISK in the sense that if the container is running
        # untrusted code, it can now synthesize keystrokes on a host
        # terminal and escape.
        #
        # We leave it like this for a few reasons:
        #   - We currently only build trusted code with the toolchain.
        #     Securing it against untrusted code is a big effort, covering
        #     more than just this specific vulnerability.
        #   - Being able to use container invocations in pipelines is
        #     very useful.
        #   - In the boot case, we `nsenter`, which is subject to the
        #     same attack. We don't have code to interpose a PTY there.
        #   - If we wanted to mitigate the risk, we could later do so:
        #       * Add an `--interactive` mode that interposes a PTY, for
        #         when the user wants that.  Default to that when no command
        #         is given to the CLI, otherwise use `--non-interactive`.
        #       * In non-interactive mode, replace FDs 0/1/2 with
        #         something that interposes a pipe -- i.e. instead of
        #         `/dev/pts/0`, the container sees `[pipe]`, which
        #        is `splice`d to write to the original PTY.
        #     In fact, the mitigation really belongs in `systemd-nspawn`, we
        #     may yet propose it to upstream.
        cmd.append('--console=pipe')

    assert setup.popen_args.boot_console is None, setup  # Should be unset
    cmd_popen = functools.partial(
        # NB: stdout is stderr if stdout is None, this is also our contract.
        setup.subvol.popen_as_root,
        check=setup.popen_args.check,
        env=setup.nspawn_env,  # `cmd_env` is set via `nspawn` args
        stdin=setup.popen_args.stdin,
        stdout=setup.popen_args.stdout,
        stderr=setup.popen_args.stderr,
    )
    with maybe_popen_and_inject_fds(
        (*cmd, '--', *opts.cmd),
        opts,
        cmd_popen,
        set_listen_fds=True,  # We must pass FDs through `systemd-nspawn`
    ) as proc:
        # NB: While we could `return` here, the caller would then need to
        # remember not to use the this `proc` as a context (since it's
        # already entered).  So instead, ensure use as a context manager.
        yield proc
