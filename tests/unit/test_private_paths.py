"""Private child directories preserve platform-native root policy and confinement."""
import os
from pathlib import Path
import stat
import tempfile
import unittest

from graphmind.files import PrivateFileStore
from graphmind.config import Settings
from graphmind.private_paths import mkdir_private


class PrivatePathsTests(unittest.TestCase):
    def test_nested_directories_are_confined_and_posix_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent/'data'
            child = root/'derived'/'stage'
            mkdir_private(child, root)
            self.assertTrue(child.is_dir())
            with self.assertRaises(FileExistsError):
                mkdir_private(child, root)
            mkdir_private(child, root, exist_ok=True)
            outside = parent/'outside'
            with self.assertRaises(ValueError):
                mkdir_private(outside, root)
            with self.assertRaises(ValueError):
                mkdir_private(root/'..'/'outside', root)
            self.assertFalse(outside.exists())
            if os.name != 'nt':
                escape = root/'escape'
                escape.symlink_to(parent, target_is_directory=True)
                with self.assertRaises(ValueError):
                    mkdir_private(escape/'outside', root)
                for directory in (root, child.parent, child):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_children_and_file_versions_preserve_the_existing_root_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/'data'
            mkdir_private(root, root)
            if os.name == 'nt':
                import ntsecuritycon
                import win32security
                info = win32security.DACL_SECURITY_INFORMATION
                dummy = win32security.ConvertStringSidToSid('S-1-5-21-101-202-303-9876')
                def inherited_dummy(path):
                    acl = win32security.GetFileSecurity(str(path), info).GetSecurityDescriptorDacl()
                    return [ace for ace in (acl.GetAce(i) for i in range(acl.GetAceCount())) if ace[2] == dummy]
                self.assertEqual(inherited_dummy(root), [])  # New root's 0700 is still owner-private.
                descriptor = win32security.GetFileSecurity(str(root), info)
                acl = descriptor.GetSecurityDescriptorDacl()
                acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS,
                    win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                    ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE, dummy)
                win32security.SetNamedSecurityInfo(str(root), win32security.SE_FILE_OBJECT,
                    info, None, None, acl, None)
                before = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                    win32security.GetFileSecurity(str(root), info), win32security.SDDL_REVISION_1, info)
            Settings(data_dir=root, allowed_import_roots=(Path(temporary),)).ensure_private_paths()
            store = PrivateFileStore(root, root/'files')
            self.assertTrue(store.readiness().available)
            reference = store.put_bytes('document', 'version', b'owned')
            self.assertEqual(store.read_bytes(reference, max_bytes=5), b'owned')
            mkdir_private(root/'derived'/'stage', root)
            directories = (root/'files', root/'files/document', root/'files/document/version', root/'derived', root/'derived/stage')
            if os.name == 'nt':
                for path in (*directories, root/reference):
                    entries = inherited_dummy(path)
                    self.assertTrue(entries, str(path))
                    self.assertTrue(all(ace[0][1] & win32security.INHERITED_ACE for ace in entries),
                                    str(path) + ': ' + repr([ace[0] for ace in entries]))
                after = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                    win32security.GetFileSecurity(str(root), info), win32security.SDDL_REVISION_1, info)
                self.assertEqual(after, before)
            else:
                for path in (root, *directories):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)


if __name__ == '__main__':
    unittest.main()
