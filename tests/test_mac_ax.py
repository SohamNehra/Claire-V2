"""Native window identity regressions; no real UI access or mutations."""
import sys
import unittest
from unittest.mock import Mock,patch

@unittest.skipUnless(sys.platform=='darwin','macOS native framework')
class NativeWindowTests(unittest.TestCase):
    def setUp(self):
        from claire_core import mac_ax
        self.ax=mac_ax

    def test_focused_application_is_not_mistaken_for_a_window(self):
        focused=Mock(spec=self.ax.Element);focused.get.return_value='AXApplication'
        app=Mock();app.get.side_effect=lambda k,d=None: [] if k=='AXWindows' else focused
        self.assertEqual(self.ax.windows(app),[])

    def test_valid_focused_window_survives_an_incomplete_window_list(self):
        focused=Mock(spec=self.ax.Element);focused.get.side_effect=lambda k,d=None: {'AXRole':'AXWindow','AXSubrole':'AXStandardWindow'}.get(k,d)
        app=Mock();app.get.side_effect=lambda k,d=None: [] if k=='AXWindows' else focused
        self.assertEqual(self.ax.windows(app),[focused])

    def test_explicit_window_selection_and_changed_identity_are_checked_before_press(self):
        first=Mock();second=Mock();second.get.return_value=None
        with patch.object(self.ax,'windows',return_value=[first,second]),patch.object(self.ax,'info',return_value={'id':42}) as info:
            with self.assertRaisesRegex(RuntimeError,'CLAIRE:CHANGED'):
                self.ax.run(Mock(),123,{'op':'click','win':1,'expected_window':7,'path':[],'role':'AXButton'})
        info.assert_called_once_with(second,123)
        first.press.assert_not_called();second.press.assert_not_called()
