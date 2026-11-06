import asyncio
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from gatewatch.actions.demo import DemoActions
from gatewatch.agent import run
from gatewatch.channels.telegram import TelegramChannel
from gatewatch.hub import create_app
from gatewatch.model import Event
from gatewatch.storage import HubStore

helper_spec = importlib.util.spec_from_file_location('action_helper',Path(__file__).resolve().parents[1]/'integrations/actions/helper.py')
helper = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(helper)


class ActionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HubStore(str(Path(self.temp.name)/'hub.db'))
        self.node,self.token = self.store.add_node('Test')
        self.store.ingest(self.node,[],[])
        self.store.control.accept(self.node,['ban','unban'],[])

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def request(self, **fields):
        return self.store.control.request(self.node,fields.get('action','ban'),fields.get('ip','203.0.113.42'),42,fields.get('key','one'),'tg',{})

    def test_commands_lease_and_result_are_node_scoped(self):
        job = self.request()
        self.assertEqual(self.request(),job)
        self.assertEqual(self.store.control.fetch(self.node)[0]['id'],job)
        self.assertEqual(self.store.control.fetch(self.node),[])
        other,_ = self.store.add_node('Other')
        result = {'id':job,'ok':True,'detail':'done'}
        self.assertEqual(self.store.control.accept(other,[],[result]),[])
        self.assertEqual(self.store.control.accept(self.node,['ban','unban'],[result]),[job])
        self.assertEqual(json.loads(self.store.control.due('tg')['result']),result)

    def test_disabled_offline_and_unconfigured_nodes_reject_actions(self):
        for condition in ('offline','disabled','unsupported'):
            with self.subTest(condition=condition):
                with self.store.db:
                    self.store.db.execute('UPDATE nodes SET seen=?,enabled=? WHERE id=?',(time.time()-200 if condition=='offline' else time.time(),0 if condition=='disabled' else 1,self.node))
                self.store.control.accept(self.node,[] if condition=='unsupported' else ['ban'],[])
                with self.assertRaises(ValueError):
                    self.request(key=condition)

    def test_ban_waits_for_related_failure_even_when_ban_arrived_first(self):
        ban = Event.new('access.ban','203.0.113.42')
        failure = Event.new('auth.failure',ban.ip)
        self.store.ingest(self.node,[ban,failure],['tg'])
        with self.store.db:
            self.store.db.execute('UPDATE deliveries SET next_try=0')
        card = self.store.due('tg')
        self.assertEqual(card['kind'],'auth.failure')
        self.store.done(card,'tg',{'message':1})
        self.assertEqual(self.store.due('tg')['kind'],'access.ban')

    def test_unrelated_failure_does_not_hold_up_ban(self):
        self.store.ingest(self.node,[Event.new('access.ban','203.0.113.42'),Event.new('auth.failure','203.0.113.43')],['tg'])
        with self.store.db:
            self.store.db.execute('UPDATE deliveries SET next_try=0')
        self.assertEqual(self.store.due('tg')['kind'],'access.ban')

    def test_fast_unban_waits_until_ban_notification(self):
        self.store.ingest(self.node,[Event.new('access.ban','203.0.113.42'),Event.new('access.unban','203.0.113.42')],['tg'])
        self.assertIsNone(self.store.due('tg'))
        with self.store.db:
            self.store.db.execute('UPDATE deliveries SET next_try=0')
        card = self.store.due('tg')
        self.assertEqual(card['kind'],'access.ban')
        self.store.done(card,'tg',{})
        self.assertEqual(self.store.due('tg')['kind'],'access.unban')

    def test_password_history_counts_distinct_values_and_is_bounded(self):
        timestamp = time.time()
        events = [Event(f'event-password-{i:08d}','auth.failure',timestamp,'203.0.113.42',method='password',password=f'pass-{i}') for i in range(12)]
        self.store.ingest(self.node,events,['tg'])
        self.store.ingest(self.node,[Event.new('auth.failure','203.0.113.42',method='password',password='pass-11')],['tg'])
        payload = json.loads(self.store.due('tg')['payload'])
        self.assertEqual(len(payload['passwords']),8)
        self.assertEqual(payload['passwords'][0],{'value':'pass-11','count':2})
        self.assertEqual(payload['count'],13)


class HelperTest(unittest.TestCase):
    def test_protected_addresses_and_injection_never_execute(self):
        with patch.object(helper.subprocess,'run') as execute:
            for ip in ('127.0.0.1','::1','169.254.1.1','203.0.113.42','1.1.1.1; reboot'):
                result = helper.execute({'action':'ban','ip':ip},{'trusted_ips':['203.0.113.42/32']})
                self.assertFalse(result['ok'])
            execute.assert_not_called()

    def test_only_fixed_fail2ban_command_is_executed(self):
        with patch.object(helper.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertTrue(helper.execute({'action':'ban','ip':'198.51.100.42'},{'trusted_ips':[]})['ok'])
            self.assertEqual(execute.call_args.args[0],['/usr/bin/fail2ban-client','set','sshd','banip','198.51.100.42'])
            self.assertNotIn('shell',execute.call_args.kwargs)


class CallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HubStore(str(Path(self.temp.name)/'hub.db'))
        node,_ = self.store.add_node('Test')
        self.store.ingest(node,[],[])
        self.store.control.accept(node,['ban','unban'],[])
        self.node = dict(self.store.db.execute('SELECT * FROM nodes WHERE id=?',(node,)).fetchone())
        self.channel = TelegramChannel({'token':'123:fake','chat_id':-100123,'owner_ids':[42]},self.store,'tg')
        self.store.set_state(self.channel.topic_key(node),{'id':11,'name':'Test'})
        self.channel.call = AsyncMock(return_value={'message_id':5})
        event = Event.new('auth.failure','203.0.113.42')
        await self.channel.deliver(self.node,{**event.wire(),'users':['root'],'count':1,'first':event.time,'last':event.time},None)
        self.button = self.channel.call.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.channel.call.reset_mock()

    async def asyncTearDown(self):
        await self.channel.close()
        self.store.db.close()
        self.temp.cleanup()

    def query(self,user=42,topic=11):
        return {'id':'callback-1','from':{'id':user},'data':self.button,
                'message':{'chat':{'id':-100123},'message_id':5,'message_thread_id':topic}}

    async def test_nonowner_and_moved_topic_cannot_request_ban(self):
        await self.channel.callback(self.query(user=99))
        await self.channel.callback(self.query(topic=12))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],0)

    async def test_owner_callback_is_durable_and_result_flips_button(self):
        await self.channel.callback(self.query())
        job = dict(self.store.db.execute('SELECT * FROM actions').fetchone())
        self.assertEqual(job['ip'],'203.0.113.42')
        self.store.control.accept(self.node['id'],['ban','unban'],[{'id':job['id'],'ok':True,'detail':'IP blocked'}])
        job = dict(self.store.control.due('tg'))
        await self.channel.action_result(job)
        button = self.channel.call.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(button['text'],'Разблокировать')
        self.assertTrue(button['callback_data'].startswith('gw:unban:'))

    async def test_other_jail_cannot_offer_ssh_unban(self):
        menu={'node':self.node['id'],'ip':'203.0.113.42','topic':11,'kind':'access.ban','service':'nginx-http-auth'}
        markup=self.channel.keyboard('a'*24,menu,self.node)
        self.assertFalse(any(button['callback_data'].startswith(('gw:ban:','gw:unban:')) for row in markup['inline_keyboard'] for button in row))


class ControlPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_node_longpoll_executes_once_and_acknowledges(self):
        class Channel:
            def __init__(self,*args): pass
            async def deliver(self,*args): return {}
            async def close(self): pass

        class Source:
            def __init__(self,*args): pass
            async def records(self,cursor):
                yield 'one',[]
                while True: await asyncio.sleep(100)

        action = DemoActions({})
        action.execute = AsyncMock(wraps=action.execute)
        with tempfile.TemporaryDirectory() as temp:
            store = HubStore(str(Path(temp)/'hub.db'))
            node,token = store.add_node('Test')
            with patch('gatewatch.hub.plugin',return_value=Channel):
                app = create_app({'database':'unused','channels':{'test':{'type':'fake'}}},store)
            server = TestServer(app)
            await server.start_server()
            config = {'database':str(Path(temp)/'agent.db'),'endpoint':str(server.make_url('/')),'node_id':node,'token':token,
                      'sources':{'test':{'type':'fake'}},'actions':{'type':'demo'}}
            def loader(group,name): return Source if group=='sources' else lambda options: action
            with patch('gatewatch.agent.plugin',side_effect=loader):
                task = asyncio.create_task(run(config))
                try:
                    for _ in range(100):
                        if store.control.capabilities(node): break
                        await asyncio.sleep(0.02)
                    job = store.control.request(node,'ban','203.0.113.42',42,'one','test',{})
                    for _ in range(150):
                        row = store.db.execute('SELECT result FROM actions WHERE id=?',(job,)).fetchone()
                        if row[0]: break
                        await asyncio.sleep(0.02)
                    self.assertTrue(json.loads(row[0])['ok'])
                    self.assertEqual(action.execute.await_count,1)
                finally:
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
            await server.close()
