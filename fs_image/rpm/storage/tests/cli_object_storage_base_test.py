#!/usr/bin/env python3
import subprocess
import unittest.mock

from .storage_base_test import Storage, StorageBaseTestCase

from .. import storage


class CLIObjectStorageBaseTestCase(StorageBaseTestCase):

    def _test_write_and_read_back(self, storage_type: Storage):
        old_make_storage_id = storage_type._make_storage_id
        with unittest.mock.patch.object(
            storage_type, '_make_storage_id',
            side_effect=lambda: self._decorate_id(old_make_storage_id()),
        ):
            # To avoid slowing the test too much, eagerly start each remove
            # and let it run in the background while the test proceeds.
            with self.storage.remover() as rm:
                for _, sid in self.check_storage_impl(
                    self.storage,
                    mul=1_234_567,
                    skip_empty_writes=False
                ):
                    rm.remove(sid)

    def _test_uncommited(self, storage_type: Storage):
        fixed_sid = self._decorate_id(storage_type._make_storage_id())
        with unittest.mock.patch.object(
            storage_type, '_make_storage_id', return_value=fixed_sid,
        ) as mock:
            with self.assertRaisesRegex(RuntimeError, '^abracadabra$'):
                with self.storage.writer() as out:
                    out.write(b'boohoo')
                    # Test our exception-before-commit handling
                    raise RuntimeError('abracadabra')

            with self.storage.writer() as out:
                # No commit, so this will not get written!
                out.write(b'foobar')

            self.assertEqual([(), ()], mock.call_args_list)

            proc = subprocess.run(self.storage._cmd(
                path=self.storage._path_for_storage_id(fixed_sid),
                operation='exists',
            ), env=self.storage._configured_env(), stdout=subprocess.PIPE)
            self.assertEqual(1, proc.returncode)

            return proc

    def _test_error_cleanup(self, storage_kind: str):
        # Without a commit, all our failed cleanup is "behind the
        # scenes", and even though it errors and logs, it does not raise
        # an externally visible exception:
        with self.assertLogs(storage.__name__, level='ERROR') as cm:
            with Storage.make(key='test', kind=storage_kind).writer() as out:
                out.write(b'triggers error cleanup via commit-to-delete')
        msg, = cm.output
        self.assertRegex(msg, r'Error retrieving ID .* uncommitted blob\.')

        # If we do try to commit, the `s3` error will be raised.
        with Storage.make(key='test', kind=storage_kind).writer() as out:
            out.write(b'something')
            with self.assertRaises(subprocess.CalledProcessError):
                out.commit()