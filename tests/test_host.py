"""Cross-platform contracts plus actual Windows known-folder acceptance."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from claire_core import guard, host, resources
from claire_core.hub import Hub
from claire_core.tools import ToolContext
from claire_core.tools.local import local_files
from tests.helpers import Env


class HostContracts(unittest.TestCase):
    def test_mac_keeps_the_existing_folder_layout_and_never_calls_windows(self):
        home = os.path.expanduser('~')
        expected = {'root': os.path.abspath(os.sep), 'home': home,
                    **{name.casefold(): os.path.join(home, name) for name in host.FOLDERS}}
        with patch.object(host, 'sys', SimpleNamespace(platform='darwin')), \
                patch.object(host, 'windows_known_folder', side_effect=AssertionError('Windows API on Mac')):
            self.assertEqual(host.locations(), expected)
            self.assertEqual(host.resolve_location('~/Desktop'), '~/Desktop')
            self.assertEqual(host.resolve_location('Desktop'), expected['desktop'])
            self.assertEqual(host.user_file_roots(), (os.path.realpath(home), '/private/tmp', '/private/var/folders'))

    def test_windows_uses_redirected_unicode_folders_without_guessing_onedrive_name(self):
        desktop = os.path.join(os.path.expanduser('~'), 'OneDrive - Example', 'Bureau Δ')
        with patch.object(host, 'sys', SimpleNamespace(platform='win32')), \
                patch.object(host, 'windows_known_folder', return_value=desktop):
            known = host.locations()
            for alias in ('Desktop', 'desktop', '~/Desktop', '~\\Desktop'):
                self.assertEqual(host.resolve_location(alias, known), desktop)
            self.assertEqual(known['videos'], known['movies'])

    def test_existing_explicit_legacy_path_is_never_redirected(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = os.path.join(directory, 'Desktop')
            os.mkdir(legacy)
            known = {'home': directory, 'desktop': os.path.join(directory, 'other')}
            with patch.object(host, 'sys', SimpleNamespace(platform='win32')):
                self.assertEqual(host.resolve_location(legacy, known), legacy)

    def test_a_failed_windows_folder_lookup_does_not_invent_or_create_a_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host, 'sys', SimpleNamespace(platform='win32')), \
                patch.object(host.os.path, 'expanduser', return_value=directory), \
                patch.object(host, 'windows_known_folder', side_effect=OSError('unavailable')):
            self.assertNotIn('desktop', host.locations())
            self.assertEqual(os.listdir(directory), [])

    def test_containment_catches_sibling_prefix_and_parent_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            project = os.path.join(directory, 'work')
            self.assertTrue(host.is_within(os.path.join(project, 'nested', '..', 'file'), project))
            self.assertFalse(host.is_within(project + '-other', project))
            self.assertFalse(host.is_within(os.path.join(project, '..', 'outside'), project))

    def test_windows_memory_uses_kernel_values_and_mac_branch_is_unchanged(self):
        resources._cache = (0, None)
        self.addCleanup(setattr, resources, '_cache', (0, None))
        with patch.object(resources, 'sys', SimpleNamespace(platform='win32')), \
                patch.object(resources, 'windows_memory', return_value=(32*resources.GIB, 12*resources.GIB)):
            self.assertEqual(resources.memory_snapshot()['available_bytes'], 12*resources.GIB)


@unittest.skipUnless(sys.platform == 'win32', 'Actual Windows filesystem and OS APIs')
class WindowsAcceptance(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.hub = Hub(self.env.core, start_brain=False)
        self.addCleanup(self.hub.stop)
        self.session = self.hub._session('home')
        self.ctx = ToolContext(self.session)

    def test_real_desktop_is_os_reported_and_can_be_listed_without_absolute_path(self):
        try:
            desktop = host.windows_known_folder('Desktop')
        except OSError:
            self.skipTest('This Windows account has no Desktop known folder')
        if not os.path.isdir(desktop):
            self.skipTest('This Windows account has no Desktop directory')
        for raw in ('Desktop', '~/Desktop', os.path.join(os.path.expanduser('~'), 'Desktop')):
            result = local_files(self.ctx, {'action': 'list_directory', 'path': raw})
            expected = raw if os.path.isdir(raw) else desktop
            if raw in ('Desktop', '~/Desktop'):
                expected = desktop
            self.assertIn('Verified: listed ' + expected, result)

    def test_unicode_redirected_folder_can_be_read_but_private_state_cannot(self):
        redirected = os.path.join(self.env.root, 'OneDrive - Team', 'Bureau Δ')
        os.makedirs(redirected)
        with open(os.path.join(redirected, 'hello.txt'), 'w', encoding='utf-8') as out:
            out.write('Bonjour Δ')
        known = {'root': os.path.abspath(os.sep), 'home': os.path.expanduser('~'), 'desktop': redirected}
        with patch.object(host, 'locations', return_value=known):
            self.assertIn('Bonjour Δ', local_files(self.ctx, {'action':'read_text', 'path':'~/Desktop/hello.txt'}))
            self.assertTrue(local_files(self.ctx, {'action':'list_directory', 'path':self.env.core.settings.state_dir}).startswith('REFUSED:'))

    def test_drive_root_and_case_aliases_do_not_bypass_protection(self):
        root = os.path.splitdrive(self.env.work)[0] + os.sep
        self.assertIn('drive root', self.hub.resolve_workdir(root)[1])
        protected = self.env.core.settings.state_dir.swapcase()
        self.assertIsNotNone(self.hub.resolve_workdir(protected)[1])
        with self.assertRaises(guard.PathEscape):
            guard.safe_path(self.env.root, protected, self.env.core.settings)

    def test_user_project_outside_profile_is_accepted_and_filed(self):
        # Simulate a redirected profile boundary while using a disposable repo.
        path = self.env.work
        with patch('claire_core.hub.os.path.expanduser', side_effect=lambda p: 'Z:\\other-profile' if p == '~' else p):
            self.assertEqual(self.hub.resolve_workdir(path), (os.path.realpath(path), None))
        row = self.env.core.projects.for_task(path)
        self.assertEqual(row['path'], os.path.realpath(path))

    def test_windows_process_and_deletion_commands_cannot_skip_review(self):
        self.assertEqual(guard.classify('Remove-Item -Recurse ./old').level, 'ask')
        self.assertEqual(guard.classify('Stop-Process -Id 123').level, 'ask')
        self.assertEqual(guard.classify('Clear-Disk -Number 0').level, 'deny')

    def test_catalogue_marks_mac_only_integrations_unavailable(self):
        providers = {p['provider']:p for p in self.env.core.connectors.catalogue()}
        for name in ('mac', 'notes', 'browser'):
            self.assertFalse(providers[name]['available'])
            self.assertTrue(providers[name]['unavailable_reason'])
        self.assertTrue(providers['api']['available'])
        self.assertTrue(providers['mcp']['available'])


class WindowsBootstrapTests(unittest.TestCase):
    def setUp(self):
        from scripts import windows_model
        self.bootstrap = windows_model
        self.env = Env()
        self.addCleanup(self.env.close)
        settings = patch.object(windows_model, 'Settings', return_value=self.env.core.settings)
        settings.start()
        self.addCleanup(settings.stop)

    def test_single_model_is_selected_with_bounded_context(self):
        from claire_core import models
        with patch.object(models, 'lmstudio_library', return_value=[{'key':'local/qwen', 'name':'Local Qwen', 'context':262144}]):
            self.bootstrap.configure()
        saved = models.load(self.env.core.settings)
        self.assertEqual(saved['models'][saved['active']]['model'], 'local/qwen')
        self.assertEqual(saved['models'][saved['active']]['context'], 32768)

    def test_existing_choice_is_preserved_without_contacting_lmstudio(self):
        from claire_core import models
        entry = models.add(self.env.core.settings, 'lmstudio', model='existing')
        models.choose(self.env.core.settings, entry['name'])
        with patch.object(models, 'lmstudio_library', side_effect=AssertionError('existing choice must be preserved')):
            self.bootstrap.configure()
        self.assertEqual(models.load(self.env.core.settings)['active'], entry['name'])

    def test_several_models_are_left_for_the_user_to_choose(self):
        from claire_core import models
        with patch.object(models, 'lmstudio_library', return_value=[{'key':'a'},{'key':'b'}]):
            self.bootstrap.configure()
        self.assertEqual(models.load(self.env.core.settings)['models'], {})

    def test_windows_timezone_is_resolved_without_a_personal_launcher_setting(self):
        from claire_core import scheduler
        with patch.object(scheduler, 'sys', SimpleNamespace(platform='win32')), \
                patch.dict(os.environ, {'TZ':''}), \
                patch.dict(sys.modules, {'tzlocal': SimpleNamespace(get_localzone_name=lambda: 'Europe/London')}):
            self.assertEqual(scheduler.local_timezone(), 'Europe/London')


if __name__ == '__main__':
    unittest.main()
