#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import pwd

from dataclasses import dataclass

from fs_image.fs_utils import open_for_read_decompress
from fs_image.nspawn_in_subvol.args import new_nspawn_opts, PopenArgs
from fs_image.nspawn_in_subvol.non_booted import run_non_booted_nspawn
from fs_image.subvol_utils import Subvol

from fs_image.compiler.requires_provides import (
    ProvidesDirectory, ProvidesFile, require_directory
)

from .common import (
    coerce_path_field_normal_relative, ImageItem, LayerOpts,
    make_path_normal_relative, generate_work_dir
)


@dataclass(init=False, frozen=True)
class TarballItem(ImageItem):
    into_dir: str
    source: str
    force_root_ownership: bool

    @classmethod
    def customize_fields(cls, kwargs):
        super().customize_fields(kwargs)
        coerce_path_field_normal_relative(kwargs, 'into_dir')
        assert kwargs['force_root_ownership'] in [True, False], kwargs

    def provides(self):
        # We own ZST decompression, tarfile handles other gz, bz2, etc.
        import tarfile  # Lazy since only this method needs it.
        with open_for_read_decompress(self.source) as tf, \
                tarfile.open(fileobj=tf, mode='r|') as f:
            for item in f:
                path = os.path.join(
                    self.into_dir, make_path_normal_relative(item.name),
                )
                if item.isdir():
                    # We do NOT provide the installation directory, and the
                    # image build script tarball extractor takes pains (e.g.
                    # `tar --no-overwrite-dir`) not to touch the extraction
                    # directory.
                    if os.path.normpath(
                        os.path.relpath(path, self.into_dir)
                    ) != '.':
                        yield ProvidesDirectory(path=path)
                else:
                    yield ProvidesFile(path=path)

    def requires(self):
        yield require_directory(self.into_dir)

    def build(self, subvol: Subvol, layer_opts: LayerOpts):
        assert (layer_opts.build_appliance is not None), (
            f'`image_layer` {layer_opts.layer_target} must set '
            '`build_appliance`'
        )
        build_appliance = layer_opts.build_appliance
        work_dir = generate_work_dir()
        tar_cmd = ' '.join([
            'tar',
            # Future: Bug: `tar` unfortunately FOLLOWS existing symlinks
            # when unpacking.  This isn't dire because the compiler's
            # conflict prevention SHOULD prevent us from going out of
            # the subvolume since this TarballItem's provides would
            # collide with whatever is already present.  However, it's
            # hard to state that with complete confidence, especially if
            # we start adding support for following directory symlinks.
            '-C', work_dir + '/' + self.into_dir,
            '-x',
            # preserving xattrs need to be specified on both sides (packing
            # and unpacking)
            '--xattrs',
            # Block tar's weird handling of paths containing colons.
            '--force-local',
            # The uid:gid doing the extraction is root:root, so by default
            # tar would try to restore the file ownership from the archive.
            # In some cases, we just want all the files to be root-owned.
            *(['--no-same-owner'] if self.force_root_ownership else []),
            # The next option is an extra safeguard that is redundant
            # with the compiler's prevention of `provides` conflicts.
            # It has two consequences:
            #
            #  (1) If a file already exists, `tar` will fail with an error.
            #      It is **not** an error if a directory already exists --
            #      otherwise, one would never be able to safely untar
            #      something into e.g. `/usr/local/bin`.
            #
            #  (2) Less obviously, the option prevents `tar` from
            #      overwriting the permissions of `directory`, as it
            #      otherwise would.
            #
            #      Thanks to the compiler's conflict detection, this should
            #      not come up, but now you know.  Observe us clobber the
            #      permissions without it:
            #
            #        $ mkdir IN OUT
            #        $ touch IN/file
            #        $ chmod og-rwx IN
            #        $ ls -ld IN OUT
            #        drwx------. 2 lesha users 17 Sep 11 21:50 IN
            #        drwxr-xr-x. 2 lesha users  6 Sep 11 21:50 OUT
            #        $ tar -C IN -czf file.tgz .
            #        $ tar -C OUT -xvf file.tgz
            #        ./
            #        ./file
            #        $ ls -ld IN OUT
            #        drwx------. 2 lesha users 17 Sep 11 21:50 IN
            #        drwx------. 2 lesha users 17 Sep 11 21:50 OUT
            #
            #      Adding `--keep-old-files` preserves `OUT`'s metadata:
            #
            #        $ rm -rf OUT ; mkdir out ; ls -ld OUT
            #        drwxr-xr-x. 2 lesha users 6 Sep 11 21:53 OUT
            #        $ tar -C OUT --keep-old-files -xvf file.tgz
            #        ./
            #        ./file
            #        $ ls -ld IN OUT
            #        drwx------. 2 lesha users 17 Sep 11 21:50 IN
            #        drwxr-xr-x. 2 lesha users 17 Sep 11 21:54 OUT
            '--keep-old-files',
            '-f', '-',
        ])
        with open_for_read_decompress(self.source) as tf:
            opts = new_nspawn_opts(
                # '0<&3' below redirects fd=3 to stdin, so 'tar ... -f -' will
                # read and unpack whatever we represent as fd=3. We pass `tf` as
                # fd=3 into container by 'forward_fd=...' below. See help
                # string in fs_image/nspawn_in_subvol/args.py where
                # _parser_add_nspawn_opts() calls
                # parser.add_argument('--forward-fd')
                cmd=['sh', '-uec', f'{tar_cmd} 0<&3'],
                layer=build_appliance,
                bindmount_rw=[(subvol.path(), work_dir)],
                user=pwd.getpwnam('root'),
                forward_fd=[tf.fileno()],
                allow_mknod=True,
            )
            run_non_booted_nspawn(opts, PopenArgs())
