"""Platform bootstrap checks use fake WMI hooks, never live WMI or services."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from crowdsim import windows_platform


class WindowsPlatformTests(unittest.TestCase):
    def test_wmi_hook_uses_the_existing_oserror_fallback_without_querying_wmi(self):
        original = Mock(side_effect=AssertionError('Live WMI must not be queried'))
        fake_platform = SimpleNamespace(_wmi_query=original)
        with patch.object(windows_platform, 'WINDOWS', True), patch.object(windows_platform, 'platform', fake_platform):
            self.assertTrue(windows_platform.configure_windows_platform())
            with self.assertRaises(OSError):
                fake_platform._wmi_query('OS', 'Version')
            self.assertTrue(windows_platform.configure_windows_platform())
            original.assert_not_called()

    def test_non_windows_platform_is_unchanged(self):
        original = Mock()
        fake_platform = SimpleNamespace(_wmi_query=original)
        with patch.object(windows_platform, 'WINDOWS', False), patch.object(windows_platform, 'platform', fake_platform):
            self.assertFalse(windows_platform.configure_windows_platform())
            self.assertIs(original, fake_platform._wmi_query)

    def test_python_without_the_private_hook_is_unchanged(self):
        fake_platform = SimpleNamespace()
        with patch.object(windows_platform, 'WINDOWS', True), patch.object(windows_platform, 'platform', fake_platform):
            self.assertFalse(windows_platform.configure_windows_platform())
            self.assertFalse(hasattr(fake_platform, '_wmi_query'))
