import copy
import json
import threading
import unittest
from unittest.mock import Mock,patch

from claire_core import boot
from claire_core.computer import ComputerError,checked_url
from claire_core.connectors import ConnectorError
from claire_core.engine import Session
from claire_core.llm import ChatResult,ModelError
from claire_core.tools import ToolContext
from claire_core.vision import inspect_images
from tests.helpers import Env


class ComputerTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        platform=patch('claire_core.connectors.mac.sys',SimpleNamespace(platform='darwin'))
        platform.start();self.addCleanup(platform.stop)
        self.env=Env();self.addCleanup(self.env.close)
        self.core=self.env.core
        self.iid=self.core.connectors.create('mac','Mac apps',{'apps':'Google Chrome, TextEdit'})['id']
        self.core.store.update_integration(self.iid,status='connected',account_label='Fixture apps')
        self.session=self.env.session(kind='home',role='talk',profile='controller')
        self.ctx=ToolContext(self.session)
        self.provider=self.core.connectors.providers['mac']
        self.provider.claim=Mock()
        self.initial={'window':{'id':51,'title':'Original','x':0,'y':0,'w':900,'h':600},
                      'tabs':[{'title':'Original','selected':True}],'url':'https://original.example'}
        self.state=copy.deepcopy(self.initial)
        self.sent=[]
        self.provider._run=Mock(side_effect=self.native)

    def native(self,**args):
        self.sent.append(args)
        op=args['op']
        if 'expected_tabs' in args and args['expected_tabs']!=[t['title'] for t in self.state['tabs']]:
            raise ConnectorError('the window changed since your snapshot')
        if args.get('expected_window') is not None and args['expected_window']!=self.state['window']['id']:
            raise ConnectorError('the window changed since your snapshot')
        if op in ('open','key','type','click','set_value','click_at'):return {'ok':True}
        if op=='window':return copy.deepcopy(self.state['window'])
        if op=='browser_state':return copy.deepcopy(self.state)
        if op=='browser_new':
            before=copy.deepcopy(self.state)
            for t in self.state['tabs']:t['selected']=False
            self.state['tabs'].append({'title':'New Tab','selected':True})
            self.state['window']['title']='New Tab'
            return {'before':before,'after':copy.deepcopy(self.state),'index':len(self.state['tabs'])-1,'verified':True}
        if op=='browser_navigate':
            self.state['url']=args['url'];self.state['window']['title']='Google'
            self.state['tabs'][args['expected_index']]['title']='Google'
            return {'after':copy.deepcopy(self.state),'verified':True,'performed':True}
        if op=='browser_close_owned':
            self.state['tabs'].pop(args['expected_index']);self.state['tabs'][0]['selected']=True
            self.state['window']['title']=self.state['tabs'][0]['title']
            return {'after':copy.deepcopy(self.state),'verified':True,'performed':True}
        if op=='snapshot':
            return {'window':copy.deepcopy(self.state['window']),'title':self.state['window']['title'],'full':False,
                    'nodes':[{'path':[0],'role':'AXButton','name':'Save','desc':'','value':'','enabled':True,'depth':0},
                             {'path':[1],'role':'AXTextArea','name':'Editor','desc':'','value':'draft','enabled':True,'depth':0}]}
        raise AssertionError(op)

    def call(self,action,**args):
        return self.core.computer.call(self.ctx,{'action':action,'app':'Google Chrome',**args})

    def test_atomic_tab_workflow_verifies_and_preserves_existing_tabs_without_asking_to_close_its_own_tab(self):
        created=self.call('new_tab',url='google.com')
        self.assertTrue(created['verified']);self.assertTrue(created['performed'])
        closed=self.call('close_tab',tab=created['tab'])
        self.assertTrue(closed['verified'])
        self.assertEqual(self.state['tabs'],self.initial['tabs'])
        self.assertEqual(self.env.asked,[])

    def test_reset_invalidates_scoped_refs_and_last_receipts(self):
        ref=self.call('new_tab')['tab']
        self.call('inspect')
        self.core.computer.forget(self.session.thread_id)
        self.assertNotIn(ref,self.core.computer.tabs)
        self.assertIsNone(self.core.computer.state(self.session))
        self.assertFalse(self.core.computer.observations)

    def test_visual_specialist_gets_present_question_not_future_action_instructions(self):
        analysis={'window':copy.deepcopy(self.initial['window']),'text':'orange circle','source':'actual screenshot'}
        self.session._active_request='Click the orange circle, then read the resulting text.'
        with patch('claire_core.vision.inspect_window',return_value=analysis) as inspect:
            self.call('look',query='Locate the orange circle center on a 0-1000 grid.')
        question=inspect.call_args.args[4]
        self.assertNotIn('then read',question)
        self.assertIn('center',question)

    def test_numeric_coordinate_pair_can_be_adapted_without_accepting_arbitrary_target_text(self):
        analysis={'window':copy.deepcopy(self.initial['window']),'text':'orange circle','source':'actual screenshot'}
        with patch('claire_core.vision.inspect_window',return_value=analysis):
            view=self.call('look',query='Circle center')['observation']
        self.provider.review=Mock(return_value=None)
        result=self.call('click_at',observation=view,target='420,395')
        self.assertTrue(result['performed'])
        click=next(a for a in self.sent if a['op']=='click_at')
        self.assertEqual((click['x'],click['y']),(378.0,237.0))

    def test_existing_tab_closure_still_requires_approval(self):
        ref=self.call('tabs')['tabs'][0]['tab']
        self.env.allow=False
        with self.assertRaisesRegex(ComputerError,'User denied'):
            self.call('close_tab',tab=ref)
        self.assertEqual(len(self.env.asked),1)
        self.assertEqual(self.state['tabs'],self.initial['tabs'])

    def test_changed_tab_list_is_never_closed_blindly(self):
        ref=self.call('new_tab')['tab']
        self.state['tabs'].insert(0,{'title':'User added a tab','selected':False})
        with self.assertRaisesRegex(ComputerError,'window changed'):
            self.call('close_tab',tab=ref)
        self.assertEqual(len(self.state['tabs']),3)

    def test_reference_cannot_be_used_from_another_conversation(self):
        ref=self.call('new_tab')['tab']
        other=self.env.session(kind='home',role='talk',profile='controller')
        with self.assertRaisesRegex(ComputerError,'not issued to this conversation'):
            self.core.computer.call(ToolContext(other),{'action':'close_tab','tab':ref})
        self.assertEqual(len(self.state['tabs']),2)

    def test_window_replacement_is_refused(self):
        ref=self.call('new_tab')['tab'];self.state['window']['id']=99
        with self.assertRaisesRegex(ComputerError,'window changed'):
            self.call('close_tab',tab=ref)

    def test_duplicate_close_cannot_touch_the_next_user_tab(self):
        ref=self.call('new_tab')['tab'];self.call('close_tab',tab=ref)
        count=len(self.sent)
        with self.assertRaises(ComputerError):self.call('close_tab',tab=ref)
        self.assertEqual(len(self.sent),count)

    def test_dangerous_urls_fail_before_any_ui_action(self):
        for raw in ('javascript:alert(1)','file:///etc/passwd','data:text/html,test','https://user:pass@example.com','google.com\nrm -rf /'):
            with self.assertRaises(ComputerError):self.call('new_tab',url=raw)
        self.assertEqual(self.sent,[])
        self.assertEqual(checked_url('localhost:3000/page'),'https://localhost:3000/page')

    def test_new_tab_is_reported_when_navigation_fails_after_creation(self):
        real=self.native
        def failed(**args):
            if args['op']=='browser_navigate':raise ConnectorError('network unavailable')
            return real(**args)
        self.provider._run.side_effect=failed
        with self.assertRaises(ComputerError) as error:self.call('new_tab',url='google.com')
        self.assertTrue(error.exception.performed)
        self.assertIn('tab',error.exception.details)
        self.assertEqual(len(self.state['tabs']),2)

    def test_unverified_navigation_receipt_does_not_claim_the_page_loaded(self):
        real=self.native
        def unverified(**args):
            response=real(**args)
            if args['op']=='browser_navigate':response['verified']=False
            return response
        self.provider._run.side_effect=unverified
        result=self.call('new_tab',url='google.com')
        self.assertFalse(result['ok']);self.assertFalse(result['verified']);self.assertTrue(result['performed'])
        self.assertIn('do not open another',result['note'])

    def test_stale_inspection_and_changed_target_are_refused_before_clicking(self):
        view=self.call('inspect')['observation']
        self.core.computer.observations[view]['at']-=61
        with self.assertRaisesRegex(ComputerError,'Inspect'):self.call('click',observation=view,target='e1')
        view=self.call('inspect')['observation']
        self.state['window']['title']='Another document'
        with self.assertRaisesRegex(ComputerError,'window changed'):self.call('click',observation=view,target='e1')
        self.assertNotIn('click',[r['op'] for r in self.sent])

    def test_visual_click_requires_a_real_image_observation(self):
        view=self.call('inspect')['observation']
        with self.assertRaisesRegex(ComputerError,'Use look'):
            self.call('click_at',observation=view,x='500',y='500')

    def test_visual_click_refuses_moved_or_resized_window(self):
        with patch('claire_core.vision.inspect_window',return_value={'window':copy.deepcopy(self.state['window']),'text':'A blue circle.'}):
            view=self.call('look',query='Locate the blue circle')['observation']
        self.state['window']['w']=1200
        with self.assertRaisesRegex(ComputerError,'pictured window changed'):
            self.call('click_at',observation=view,x='500',y='500')
        self.assertNotIn('click_at',[r['op'] for r in self.sent])

    def test_unallowed_app_is_never_launched(self):
        with self.assertRaises(ComputerError):self.core.computer.call(self.ctx,{'action':'open','app':'Terminal'})
        self.assertEqual(self.sent,[])


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.env=Env();self.addCleanup(self.env.close);self.core=self.env.core
        self.core.settings.roles['vision']={'server':'lmstudio','api_base':'http://127.0.0.1:1234/v1','model':'vision'}
        self.spec={'kind':'lmstudio','url':'http://127.0.0.1:1234','model':'vision'}
        self.entry={'capabilities':{'vision':True},'size_bytes':1024**3,'loaded_instances':[]}
        self.core.llm.describe=Mock(return_value={'vision':True,'model':'vision'})
        self.core.llm.chat=Mock(return_value=ChatResult('Blue rectangle and code 7349.','',100,10))
        for p in (patch('claire_core.vision.boot.server_spec',return_value=self.spec),
                  patch('claire_core.vision.boot._lmstudio_model',side_effect=lambda _:self.entry),
                  patch('claire_core.vision.memory_snapshot',return_value={'total_bytes':64*1024**3,'available_bytes':48*1024**3})):
            p.start();self.addCleanup(p.stop)

    def test_actual_image_is_sent_to_vision_role_and_only_new_model_is_unloaded(self):
        with patch('claire_core.vision.boot.ensure_role',return_value=None),patch('claire_core.vision.boot.unload_role') as unload:
            result=inspect_images(self.core,[('QUJD','image/png')],'Read the code.')
        args,kwargs=self.core.llm.chat.call_args
        self.assertEqual(kwargs['role'],'vision')
        self.assertEqual(args[0][1]['content'][1]['image_url']['url'],'data:image/png;base64,QUJD')
        self.assertEqual(result['source'],'actual screenshot')
        unload.assert_called_once_with(self.core.settings,'vision')
        self.assertFalse(self.core.power.status()['busy'])

    def test_preloaded_model_is_not_ejected_after_our_use(self):
        self.entry['loaded_instances']=[{'id':'already-loaded'}]
        with patch('claire_core.vision.boot.ensure_role',return_value=None),patch('claire_core.vision.boot.unload_role') as unload:
            inspect_images(self.core,[('QUJD','image/png')],'Describe it.')
        unload.assert_not_called()

    def test_insufficient_ram_does_not_eject_the_coding_model_or_attempt_load(self):
        with patch('claire_core.vision.memory_snapshot',return_value={'total_bytes':64*1024**3,'available_bytes':1}), \
             patch('claire_core.vision.boot.ensure_role') as ensure,patch('claire_core.vision.boot.unload_role') as unload:
            with self.assertRaisesRegex(ComputerError,'No active coding model was ejected'):
                inspect_images(self.core,[('QUJD','image/png')],'Describe it.')
        ensure.assert_not_called();unload.assert_not_called()

    def test_unknown_model_size_fails_ram_admission_before_loading(self):
        self.entry.pop('size_bytes')
        with patch('claire_core.vision.boot.ensure_role') as ensure:
            with self.assertRaisesRegex(ComputerError,'size is unknown'):
                inspect_images(self.core,[('QUJD','image/png')],'Describe it.')
        ensure.assert_not_called()

    def test_text_only_model_never_receives_the_image(self):
        self.entry['capabilities']['vision']=False
        with self.assertRaisesRegex(ComputerError,'does not report vision'):
            inspect_images(self.core,[('QUJD','image/png')],'Describe it.')
        self.core.llm.chat.assert_not_called()

    def test_failed_inference_still_releases_loaded_vision_model(self):
        self.core.llm.chat.side_effect=ModelError('inference failed')
        with patch('claire_core.vision.boot.ensure_role',return_value=None),patch('claire_core.vision.boot.unload_role') as unload:
            with self.assertRaisesRegex(ComputerError,'inference failed'):
                inspect_images(self.core,[('QUJD','image/png')],'Describe it.')
        unload.assert_called_once();self.assertFalse(self.core.power.status()['busy'])

    def test_cancelled_request_does_not_load_model(self):
        cancel=threading.Event();cancel.set()
        with patch('claire_core.vision.boot.ensure_role') as ensure:
            with self.assertRaisesRegex(ComputerError,'cancelled'):
                inspect_images(self.core,[('QUJD','image/png')],'Describe it.',cancel)
        ensure.assert_not_called()


class VisionRoleConfigurationTests(unittest.TestCase):
    def test_native_lmstudio_vision_role_does_not_change_the_coding_model(self):
        from tests.helpers import managed_server_spec
        from claire_core.settings import Settings
        settings=Settings(roles={'vision':{'server':'lmstudio','api_base':'http://127.0.0.1:1234/v1','model':'vision-model','context':8192}})
        before=settings.model
        spec=managed_server_spec(settings,'vision')
        self.assertEqual(spec['kind'],'lmstudio')
        self.assertEqual((spec['model'],spec['url'],spec['context']),('vision-model','http://127.0.0.1:1234',8192))
        self.assertEqual(settings.model,before)
