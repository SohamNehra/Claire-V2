"""Browser receipts must follow stable tab identities and settled selection."""
import copy
import unittest
from unittest.mock import Mock,patch
from tests.helpers import Env

class NativeBrowserTests(unittest.TestCase):
    def setUp(self):
        self.env=Env();self.addCleanup(self.env.close)
        self.mac=self.env.core.connectors.providers['mac'];self.ax=Mock();self.app=Mock();self.window=Mock()
        self.mac._run=Mock();self.ax.info.return_value={'id':7}
        self.ax.windows.return_value=[self.window]
        self.before={'window':{'id':7},'url':'example.com','tabs':[{'identity':'1','title':'Original','selected':True}]}
    def state(self,data):return copy.deepcopy(data),[Mock() for _ in data['tabs']],self.window

    def test_new_tab_waits_for_the_inserted_tab_to_be_selected(self):
        intermediate=copy.deepcopy(self.before);intermediate['tabs'].append({'identity':'2','title':'New tab','selected':False})
        settled=copy.deepcopy(intermediate);settled['tabs'][0]['selected']=False;settled['tabs'][1]['selected']=True
        self.ax.browser_state.side_effect=[self.state(self.before),self.state(intermediate),self.state(settled)]
        with patch('claire_core.connectors.mac.time.sleep'):
            result=self.mac._browser_native(self.ax,self.app,23,{'op':'browser_new','app':'Google Chrome'})
        self.assertTrue(result['verified']);self.assertEqual(result['index'],1)
        self.mac._run.assert_called_once()

    def test_title_change_does_not_lose_an_owned_tab_with_the_same_identity(self):
        before=copy.deepcopy(self.before);before['tabs'][0]['selected']=False
        before['tabs'].append({'identity':'2','title':'Loaded Google title','selected':True})
        self.ax.browser_state.side_effect=[self.state(before),self.state(self.before)]
        result=self.mac._browser_native(self.ax,self.app,23,{'app':'Google Chrome','op':'browser_close_owned','expected_window':7,
            'expected_tabs':['Original','Loading...'],'expected_tab_ids':['1','2'],'expected_index':1})
        self.assertTrue(result['verified']);self.assertEqual(len(result['after']['tabs']),1)

    def test_replaced_tab_identity_refuses_before_any_shortcut(self):
        self.ax.browser_state.return_value=self.state(self.before)
        with self.assertRaisesRegex(RuntimeError,'CLAIRE:CHANGED'):
            self.mac._browser_native(self.ax,self.app,23,{'app':'Google Chrome','op':'browser_close_owned','expected_tab_ids':['replacement'],'expected_index':0})
        self.mac._run.assert_not_called()

    def test_inaccessible_existing_windows_do_not_trigger_an_extra_window(self):
        self.ax.windows.return_value=[];self.ax.has_window_candidates.return_value=True
        with self.assertRaisesRegex(RuntimeError,'CLAIRE:NO_WINDOW'):
            self.mac._browser_native(self.ax,self.app,23,{'op':'browser_new','app':'Google Chrome'})
        self.mac._run.assert_not_called()

    def test_navigation_never_types_a_url_into_a_non_address_field(self):
        self.ax.browser_state.return_value=self.state(self.before)
        field=Mock();field.get.side_effect=lambda k: {'AXRole':'AXTextField','AXTitle':'Project name','AXDescription':''}.get(k)
        self.app.get.return_value=field
        with patch('claire_core.connectors.mac.time.monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(RuntimeError,'CLAIRE:CHANGED'):
                self.mac._browser_native(self.ax,self.app,23,{'op':'browser_navigate','app':'Google Chrome','url':'https://example.com','expected_index':0})
        self.mac._run.assert_called_once()
        self.assertEqual(self.mac._run.call_args.kwargs['key'],'l')
        self.assertNotIn('url',self.mac._run.call_args.kwargs)
