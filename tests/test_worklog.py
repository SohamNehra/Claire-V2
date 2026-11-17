import concurrent.futures
import os
import time
import unittest
from unittest.mock import patch

from claire_core.hub import Hub
from claire_core.vault import Vault
from claire_core.worklog import PERIOD
from tests.helpers import Env, done
from tests.test_service import ServiceCase


class WorkLogTests(unittest.TestCase):
    def setUp(self):
        self.env=Env()
        self.addCleanup(self.env.close)
        self.vault=self.env.core.vault
        self.work=self.vault.work

    def read(self,name):
        with open(os.path.join(self.work.path,name+'.md')) as f:return f.read()

    def test_recent_work_is_separate_from_identity_and_survives_reload(self):
        before=self.vault.identity()
        self.vault.remember('work','Building a focused local assistant.',note='Claire 2',home=True)
        self.assertEqual(self.vault.identity(),before)
        other=Vault(self.vault.path,self.vault.index_path)
        self.assertIn('focused local assistant',other.load_home_memory())
        self.assertIn('focused local assistant',other.recall('Claire 2'))
        self.assertNotIn('focused local assistant',other.identity())

    def test_exact_30_day_rollover_keeps_archive_searchable(self):
        now=time.time()
        self.work.record('AncientZetaProject','Retired the v1 parser.',entry_id='old',now=now-PERIOD-1)
        self.work.record('CurrentAlphaProject','Building the new UI.',entry_id='new',now=now)
        self.work.rotate(now)
        self.assertNotIn('AncientZetaProject',self.read('recent'))
        self.assertIn('AncientZetaProject',self.read('old'))
        self.assertIn('CurrentAlphaProject',self.read('recent'))
        self.assertIn('Retired the v1 parser',self.vault.recall('AncientZetaProject'))
        self.work.rotate(now)
        self.assertEqual(self.read('old').count('"id": "old"'),1)

    def test_updated_project_stays_recent_from_last_update(self):
        now=time.time()
        self.work.record('Claire','Old plan',entry_id='same',now=now-PERIOD+1)
        self.work.record('Claire','New plan',entry_id='same',now=now)
        self.work.rotate(now+2)
        self.assertIn('New plan',self.read('recent'))
        self.assertNotIn('Old plan',self.read('recent'))
        self.assertNotIn('"id": "same"',self.read('old'))

    def test_archive_first_rollover_can_recover_after_interruption(self):
        now=time.time()
        self.work.record('Recovery','Keep this outcome',entry_id='recover',now=now-PERIOD-1)
        write=self.work._write
        def fail_recent(name,*args):
            if name=='recent':raise OSError('simulated interrupted write')
            return write(name,*args)
        with patch.object(self.work,'_write',side_effect=fail_recent):
            with self.assertRaises(OSError):self.work.rotate(now)
        self.assertIn('Keep this outcome',self.read('old'))
        self.work.rotate(now)
        self.assertEqual(self.read('old').count('"id": "recover"'),1)
        self.assertNotIn('Keep this outcome',self.read('recent'))

    def test_parallel_updates_are_not_lost(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda i:self.work.record('Project '+str(i),'Outcome '+str(i),entry_id=str(i)),range(20)))
        text=self.read('recent')
        for i in range(20):self.assertIn('"id": "'+str(i)+'"',text)

    def test_manual_text_and_invalid_metadata_are_preserved(self):
        self.work.rotate()
        with open(os.path.join(self.work.path,'recent.md'),'a') as f:
            f.write('\nPersonal editing note, keep it.\n<!-- claire-work {"id":"bad","updated":NaN} -->\nKeep this too.\n')
        self.work.rotate()
        self.assertIn('Personal editing note',self.read('recent'))
        self.assertIn('Keep this too',self.read('recent'))

    def test_open_home_refreshes_work_context_on_its_next_turn(self):
        hub=Hub(self.env.core,start_brain=False)
        home=hub._session('home')
        self.assertNotIn('Fresh project direction',home.messages[0]['content'])
        self.work.record('Claire','Fresh project direction')
        self.env.llm.replies.append(done('Acknowledged.'))
        home.run('What is current?')
        self.assertIn('Fresh project direction',self.env.llm.calls[-1]['messages'][0]['content'])

    def test_recall_snippet_reaches_matches_beyond_file_header(self):
        body='Unrelated older work.\n'*300+'UniqueLongProject shipped successfully.'
        excerpt=self.vault._excerpt(body,'UniqueLongProject')
        self.assertIn('shipped successfully',excerpt)


class WorkLogIntegrationTests(ServiceCase):
    def test_task_report_updates_work_memory_without_model_remember_call(self):
        self.boot(task=[done('Fixed the parser and passed tests.')])
        row=self.client.post('/threads',{'title':'Parser work','task':'Fix parser','workdir':self.env.work})['thread']
        self.wait_for(lambda e:e['type']=='turn_end' and e['thread']==row['id'])
        memory=self.env.core.vault.load_home_memory()
        self.assertIn('Fixed the parser and passed tests.',memory)
        self.assertIn('Status: done',memory)
        with open(os.path.join(self.env.core.vault.path,'work','recent.md')) as f:
            self.assertEqual(f.read().count('"id": "task:'+row['id']+'"'),1)
        self.assertTrue(self.env.core.store.load_messages(row['id']))
