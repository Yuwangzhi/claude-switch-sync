"""Scenario tests: real file merges with a simulated macOS application lifecycle."""
import copy
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import switch_sync as m


PROFILES = {
    'official_direct': ('subscription', 'direct', 'official'),
    'relay_direct': ('third_party', 'direct', 'relay'),
    'relay_routing': ('third_party', 'routing', 'relay'),
    'relay_aggregation': ('third_party', 'aggregation', 'relay'),
    'ds_routing': ('third_party', 'routing', 'ds'),
    'ds_aggregation': ('third_party', 'aggregation', 'ds'),
}


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = pathlib.Path(temp.name)
        self.indexes = {k: self.base / k for k in m.VALID_KINDS}
        for path in self.indexes.values():
            path.mkdir()
        self.history = self.base / 'projects'
        self.history.mkdir()
        self.transcript = self.history / 'original.jsonl'
        self.transcript.write_bytes(b'{"type":"user","message":"loop token"}\n')
        self.original_transcript = self.transcript.read_bytes()
        (self.base / 'config.json').write_text(json.dumps({
            'indexes': {k: str(v) for k, v in self.indexes.items()}}))
        self.running = True
        self.actions = []
        self.current = self.snapshot('official_direct')
        self.original_choices = {'subscription': ('claude-sonnet-test', 'low'),
                                 'third_party': ('gateway-flash', 'low')}
        for kind, (model, effort) in self.original_choices.items():
            self.put(kind, lastActivityAt=1, model=model, effort=effort)
        for patcher in (mock.patch.object(m, 'BASE', self.base),
                        mock.patch.object(m, 'emit'),
                        mock.patch.object(m, 'app_pids', side_effect=lambda: [123] if self.running else []),
                        mock.patch.object(m, 'lifecycle', side_effect=self.lifecycle),
                        mock.patch.object(m, 'stable_snapshot', side_effect=lambda: copy.deepcopy(self.current))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reset_service('official_direct')

    def snapshot(self, profile):
        kind, mode, provider = PROFILES[profile]
        return {'cli': {'kind': 'subscription', 'mode': 'direct',
                        'history_root': str(self.history), 'provider_id': 'official'},
                'desktop': {'kind': kind, 'mode': mode, 'provider_id': provider}}

    def reset_service(self, profile):
        self.current = self.snapshot(profile)
        (self.base / 'state.json').write_text(json.dumps({
            'version': 1, 'observed': self.current, 'completed_handoffs': 0}))
        self.service = m.Service()

    def put(self, kind, **changes):
        path = self.indexes[kind] / 'local_loop.json'
        record = json.loads(path.read_bytes()) if path.exists() else {
            'sessionId': 'local_loop', 'cliSessionId': 'head-0',
            'createdAt': 1, 'cwd': '/empty-test-project', 'isArchived': False}
        record.update(changes)
        path.write_text(json.dumps(record))
        return record

    def read(self, kind):
        return json.loads((self.indexes[kind] / 'local_loop.json').read_bytes())

    def lifecycle(self, command):
        self.actions.append(command)
        if command == 'quit':
            saved = json.loads((self.base / 'state.json').read_bytes())
            self.assertTrue(saved['pending']['quit_requested'])
        self.running = command == 'open'

    def change_to(self, profile):
        self.current = self.snapshot(profile)
        self.service.reconcile(copy.deepcopy(self.current))

    def assert_integrity(self):
        self.assertEqual(self.transcript.read_bytes(), self.original_transcript)
        for kind, choice in self.original_choices.items():
            record = self.read(kind)
            self.assertEqual((record['model'], record['effort']), choice)

    def daemon_once(self):
        # Exercise the real daemon's recovery of an already-merged handoff.
        # Only the OS event wait is replaced; Service and merging remain real.
        with mock.patch.object(m, 'Watcher') as watcher:
            watcher.return_value.wait.side_effect = StopIteration
            with self.assertRaises(StopIteration):
                m.daemon()
        self.service = m.Service()

    def test_repeated_loop_with_continuations_and_head_rotation(self):
        route = ['ds_routing', 'ds_aggregation', 'relay_aggregation',
                 'relay_direct', 'relay_routing', 'official_direct'] * 3
        expected_handoffs = 0
        for turn, destination in enumerate(route, 2):
            source = self.current['desktop']['kind']
            previous_record = self.read(source)
            prior_heads = previous_record.get('priorCliSessionIds', []) + [previous_record['cliSessionId']]
            self.put(source, lastActivityAt=turn, completedTurns=turn,
                     cliSessionId='head-' + str(turn), priorCliSessionIds=prior_heads,
                     title='progress-' + str(turn))
            target = PROFILES[destination][0]
            expected_handoffs += source != target
            self.change_to(destination)
            self.assert_integrity()
            self.assertEqual(self.read(target)['title'], 'progress-' + str(turn))
            self.assertTrue(self.running)
            self.assertEqual(self.service.state['completed_handoffs'], expected_handoffs)
        self.assertEqual(self.actions, ['quit', 'open'] * expected_handoffs)

    def test_cli_only_switch_leaves_desktop_running_and_indexes_unchanged(self):
        before = {k: (p / 'local_loop.json').read_bytes() for k, p in self.indexes.items()}
        self.current['cli'].update(kind='third_party', mode='routing', provider_id='ds')
        self.service.reconcile(self.current)
        self.assertEqual(self.actions, [])
        self.assertEqual(before, {k: (p / 'local_loop.json').read_bytes() for k, p in self.indexes.items()})
        self.assertEqual(self.service.state['completed_handoffs'], 0)
        self.assert_integrity()

    def test_cli_history_root_change_defers_before_desktop_quit(self):
        self.current = self.snapshot('ds_routing')
        self.current['cli'].update(kind='third_party', history_root=str(self.base / 'different'))
        with self.assertRaises(m.Deferred):
            self.service.reconcile(self.current)
        self.assertEqual(self.actions, [])
        self.assert_integrity()

    def test_partial_json_recovers_after_repair_and_natural_exit(self):
        path = self.indexes['third_party'] / 'local_loop.json'
        valid = path.read_bytes()
        path.write_text('{')
        with self.assertRaises(m.Deferred):
            self.change_to('ds_routing')
        self.assertTrue(self.running)
        self.assertEqual(self.actions, ['quit', 'open'])
        path.write_bytes(valid)
        self.service = m.Service()
        self.change_to('ds_routing')
        self.assertEqual(self.actions, ['quit', 'open'])
        self.running = False
        self.change_to('ds_routing')
        self.assertFalse(self.running)
        self.assertNotIn('pending', self.service.state)
        self.assert_integrity()

    def test_open_failure_is_retried_without_remerging_or_quitting(self):
        self.put('subscription', lastActivityAt=10, title='latest')
        def fail_open(command):
            if command == 'open':
                self.actions.append(command)
                raise m.Deferred('temporary launch failure')
            self.lifecycle(command)
        with mock.patch.object(m, 'lifecycle', side_effect=fail_open):
            with self.assertRaises(m.Deferred):
                self.change_to('ds_routing')
        event = self.service.state['last_handoff']['event_id']
        self.daemon_once()
        self.assertTrue(self.running)
        self.assertEqual(self.actions, ['quit', 'open', 'open'])
        self.assertEqual(self.service.state['last_handoff']['event_id'], event)
        self.assertEqual(self.service.state['completed_handoffs'], 1)
        self.assertNotIn('pending', self.service.state)
        self.assert_integrity()

    def test_restart_after_merge_reopens_once(self):
        self.current = self.snapshot('ds_routing')
        self.service.state.update(observed=self.current, completed_handoffs=1,
                                  pending={'phase': 'merged', 'reopen': True, 'quit_requested': True,
                                           'outgoing': 'subscription', 'target': 'third_party'})
        self.service.save()
        self.running = False
        self.daemon_once()
        self.change_to('ds_routing')
        self.assertEqual(self.actions, ['open'])
        self.assertTrue(self.running)
        self.assertEqual(self.service.state['completed_handoffs'], 1)

    def test_write_failure_keeps_backup_and_recovers_after_exit(self):
        self.put('subscription', lastActivityAt=10, title='latest')
        original_write = m.atomic_write
        writes = 0
        def fail_second_write(path, data):
            nonlocal writes
            if pathlib.Path(path).parent in self.indexes.values():
                writes += 1
                if writes == 2:
                    raise OSError('simulated disk error')
            return original_write(path, data)
        with mock.patch.object(m, 'atomic_write', side_effect=fail_second_write):
            with self.assertRaises(OSError):
                self.change_to('ds_routing')
        self.assertTrue(self.running)
        self.assertEqual(self.actions, ['quit', 'open'])
        backups = list((self.base / 'backups').glob('*/subscription/local_loop.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_bytes())['title'], 'latest')
        self.running = False
        self.change_to('ds_routing')
        self.assertFalse(self.running)
        self.assertEqual(self.read('third_party')['title'], 'latest')
        self.assertNotIn('pending', self.service.state)
        self.assert_integrity()

    def test_complete_round_trip_after_one_declined_quit(self):
        with mock.patch.object(m, 'lifecycle', side_effect=m.Deferred('quit declined')) as action:
            with self.assertRaises(m.Deferred):
                self.change_to('ds_routing')
            self.change_to('ds_aggregation')
            self.assertEqual(action.call_count, 1)
        self.change_to('official_direct')
        self.assertNotIn('pending', self.service.state)
        self.assertTrue(self.running)
        self.change_to('ds_routing')
        self.change_to('official_direct')
        self.assertEqual(self.actions, ['quit', 'open', 'quit', 'open'])
        self.assertEqual(self.service.state['completed_handoffs'], 2)
        self.assert_integrity()


def make_transition_test(source, target, opened):
    def test(self):
        self.reset_service(source)
        self.running = opened
        source_kind = PROFILES[source][0]
        target_kind = PROFILES[target][0]
        self.put(source_kind, lastActivityAt=10, title='latest progress')
        self.change_to(target)
        cross = source_kind != target_kind
        self.assertEqual(self.actions, ['quit', 'open'] if cross and opened else [])
        self.assertEqual(self.running, opened)
        self.assertEqual(self.service.state['completed_handoffs'], int(cross))
        self.assertEqual(self.read(target_kind)['title'], 'latest progress')
        self.assertNotIn('pending', self.service.state)
        self.assert_integrity()
        before = {k: (p / 'local_loop.json').read_bytes() for k, p in self.indexes.items()}
        self.change_to(target)
        self.assertEqual(before, {k: (p / 'local_loop.json').read_bytes() for k, p in self.indexes.items()})
        self.assertEqual(self.service.state['completed_handoffs'], int(cross))
    return test


for source in PROFILES:
    for target in PROFILES:
        for opened in (False, True):
            name = 'test_%s_to_%s_app_%s' % (source, target, 'open' if opened else 'closed')
            setattr(ScenarioTests, name, make_transition_test(source, target, opened))


if __name__ == '__main__':
    unittest.main(verbosity=2)
