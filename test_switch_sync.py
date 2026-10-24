import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock
import copy

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('switch_sync', HERE / 'switch_sync.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.paths = {k: self.root / k for k in m.VALID_KINDS}
        for p in self.paths.values():
            p.mkdir()
        self.config = {'indexes': {k: str(v) for k, v in self.paths.items()}}

    def tearDown(self):
        self.temp.cleanup()

    def put(self, kind, sid, activity, **extra):
        record = {'sessionId': 'local_' + sid, 'cliSessionId': sid,
                  'lastActivityAt': activity, 'completedTurns': activity,
                  'isArchived': False, **extra}
        path = self.paths[kind] / ('local_' + sid + '.json')
        path.write_text(json.dumps(record))
        return path

    def merge(self, outgoing='third_party', guard=lambda: None):
        return m.merge_indexes(self.config, outgoing, guard=guard, backup_root=self.root / 'backups')

    def test_all_mode_pairs_only_cross_auth_boundary(self):
        cases = [('subscription', 'direct'), ('third_party', 'direct'),
                 ('third_party', 'routing'), ('third_party', 'aggregation')]
        for old_kind, old_mode in cases:
            for new_kind, new_mode in cases:
                with self.subTest(old=(old_kind, old_mode), new=(new_kind, new_mode)):
                    self.assertEqual(m.crosses_boundary({'kind': old_kind, 'mode': old_mode},
                                                       {'kind': new_kind, 'mode': new_mode}), old_kind != new_kind)

    def test_active_gateway_namespace_not_copied_official_namespace(self):
        root = self.root / 'claude-code-sessions'
        for account, org in [('official-account', 'official-org'), ('gateway-account', 'gateway-org')]:
            p = root / account / org
            p.mkdir(parents=True)
            (p / 'local_a.json').write_text('{}')
        marker = root / 'gateway-account/gateway-org.profile-origin.json'
        marker.write_text(json.dumps({'mode': 'local', 'org': 'gateway-org'}))
        self.assertEqual(m.discover_index(root), root / 'gateway-account/gateway-org')

    def test_union_newest_and_preserved_backups_in_both_directions(self):
        self.put('subscription', 'a', 1)
        self.put('third_party', 'a', 2)
        self.put('third_party', 'b', 3)
        first = self.merge()
        self.assertEqual(first['session_count'], 2)
        self.assertEqual(first['conflict_count'], 1)
        for sid in ['a', 'b']:
            self.assertEqual((self.paths['subscription'] / ('local_' + sid + '.json')).read_bytes(),
                             (self.paths['third_party'] / ('local_' + sid + '.json')).read_bytes())
        self.put('subscription', 'a', 4)
        second = self.merge('subscription')
        self.assertEqual(json.loads((self.paths['third_party'] / 'local_a.json').read_bytes())['lastActivityAt'], 4)
        backup = self.root / 'backups' / second['event_id'] / 'third_party/local_a.json'
        self.assertEqual(json.loads(backup.read_bytes())['lastActivityAt'], 2)

    def test_same_timestamp_uses_the_outgoing_profile(self):
        self.put('subscription', 'a', 1, title='official rename')
        self.put('third_party', 'a', 1, title='third-party rename')
        self.merge('subscription')
        self.assertEqual(json.loads((self.paths['third_party'] / 'local_a.json').read_bytes())['title'], 'official rename')

    def test_running_app_prevents_all_metadata_writes(self):
        a = self.put('subscription', 'a', 1)
        b = self.put('third_party', 'a', 2)
        before = (a.read_bytes(), b.read_bytes())
        def running():
            raise m.Deferred('app is running')
        with self.assertRaises(m.Deferred):
            self.merge(guard=running)
        self.assertEqual(before, (a.read_bytes(), b.read_bytes()))

    def test_incomplete_json_prevents_overwriting_good_side(self):
        a = self.put('subscription', 'a', 1)
        before = a.read_bytes()
        (self.paths['third_party'] / 'local_a.json').write_text('{')
        with self.assertRaises(m.Deferred):
            self.merge()
        self.assertEqual(a.read_bytes(), before)

    def test_concurrent_edit_is_not_overwritten(self):
        a = self.put('subscription', 'a', 1)
        self.put('third_party', 'a', 2)
        calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.put('subscription', 'a', 10)
        with self.assertRaises(m.Deferred):
            self.merge(guard=guard)
        self.assertEqual(json.loads(a.read_bytes())['lastActivityAt'], 10)

    def test_deletion_markers_do_not_delete_or_resurrect_records(self):
        a = self.put('subscription', 'a', 1)
        (self.paths['third_party'] / 'deleted_a').write_text('2')
        report = self.merge()
        self.assertTrue(a.exists())
        self.assertFalse((self.paths['third_party'] / 'local_a.json').exists())
        self.assertEqual(report['files_deleted'], 0)

    def test_archive_visibility_follows_latest_record(self):
        self.put('subscription', 'a', 1, isArchived=True)
        self.put('third_party', 'a', 2, isArchived=False)
        for p in self.paths.values():
            (p / 'archived-sessions.idx').write_text(json.dumps({'v': 1, 'archived': ['local_a']}))
        self.merge()
        for p in self.paths.values():
            self.assertEqual(json.loads((p / 'archived-sessions.idx').read_text())['archived'], [])


    def test_documented_cli_head_rotation_merges_in_both_directions(self):
        for outgoing in ['subscription', 'third_party']:
            other = next(k for k in m.VALID_KINDS if k != outgoing)
            self.put(other, 'rotated', 10, cliSessionId='old-head', createdAt=1, cwd='/project')
            current = self.put(outgoing, 'rotated', 20, cliSessionId='new-head',
                               priorCliSessionIds=['old-head'], createdAt=1, cwd='/project')
            expected = current.read_bytes()
            report = self.merge(outgoing)
            self.assertEqual(report['unresolved_identity_count'], 0)
            self.assertEqual((self.paths[other] / current.name).read_bytes(), expected)
            backup = self.root / 'backups' / report['event_id'] / other / current.name
            self.assertEqual(json.loads(backup.read_bytes())['cliSessionId'], 'old-head')

    def test_independent_heads_preserve_both_copies_and_sync_other_sessions(self):
        a = self.put('subscription', 'branch', 10, cliSessionId='head-a',
                     priorCliSessionIds=['ancestor'], createdAt=1, cwd='/project', isArchived=True)
        b = self.put('third_party', 'branch', 20, cliSessionId='head-b',
                     priorCliSessionIds=['ancestor'], createdAt=1, cwd='/project', isArchived=False)
        before = (a.read_bytes(), b.read_bytes())
        self.put('subscription', 'unrelated', 3)
        report = self.merge()
        self.assertEqual(report['unresolved_identity_count'], 1)
        self.assertEqual((a.read_bytes(), b.read_bytes()), before)
        self.assertTrue((self.paths['third_party'] / 'local_unrelated.json').exists())
        for kind, expected in [('subscription', ['local_branch']), ('third_party', [])]:
            archive = json.loads((self.paths[kind] / 'archived-sessions.idx').read_bytes())
            self.assertEqual(archive['archived'], expected)
        self.assertEqual(self.merge()['writes'], 0)

    def test_lineage_requires_same_project_creation_and_explicit_predecessor(self):
        old = {'sessionId': 'local_a', 'cliSessionId': 'old', 'createdAt': 1, 'cwd': '/project'}
        new = {**old, 'cliSessionId': 'new', 'priorCliSessionIds': ['old']}
        self.assertTrue(m.has_same_transcript_lineage(new, old))
        for extra in [{'cwd': '/another'}, {'createdAt': 2}, {'createdAt': None},
                      {'priorCliSessionIds': 'old'}, {'priorCliSessionIds': ['ancestor']},
                      {'sessionId': 'local_b'}, {'cliSessionId': ''}]:
            with self.subTest(extra=extra):
                self.assertFalse(m.has_same_transcript_lineage({**new, **extra}, old))
        self.assertFalse(m.has_same_transcript_lineage(old, new))

    def test_newer_ancestor_metadata_cannot_replace_a_changed_head(self):
        a = self.put('subscription', 'a', 20, cliSessionId='old', createdAt=1, cwd='/project')
        b = self.put('third_party', 'a', 10, cliSessionId='new', priorCliSessionIds=['old'],
                     createdAt=1, cwd='/project')
        before = (a.read_bytes(), b.read_bytes())
        self.assertEqual(self.merge()['unresolved_identity_count'], 1)
        self.assertEqual((a.read_bytes(), b.read_bytes()), before)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temp.name)
        self.old = {'cli': {'kind': 'subscription', 'mode': 'direct', 'history_root': str(self.base)},
                    'desktop': {'kind': 'subscription', 'mode': 'direct', 'provider_id': 'official'}}
        self.new = copy.deepcopy(self.old)
        self.new['desktop'] = {'kind': 'third_party', 'mode': 'routing', 'provider_id': 'ds'}
        (self.base / 'config.json').write_text('{}')
        (self.base / 'state.json').write_text(json.dumps({'version': 1, 'observed': self.old,
                                                       'completed_handoffs': 0}))
        self.patchers = [mock.patch.object(m, 'BASE', self.base), mock.patch.object(m, 'emit')]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(self.temp.cleanup)
        for patcher in self.patchers:
            self.addCleanup(patcher.stop)
        self.service = m.Service()
        self.running = True
        self.actions = []
        self.report = {'event_id': 'test', 'unresolved_identity_count': 0}

    def pids(self):
        return [123] if self.running else []

    def lifecycle(self, command):
        self.actions.append(command)
        if command == 'quit':
            # The persisted guard must precede the request, not follow it.
            saved = json.loads((self.base / 'state.json').read_bytes())
            self.assertTrue(saved['pending']['quit_requested'])
        self.running = command == 'open'

    def contexts(self, merge_error=None):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.object(m, 'app_pids', self.pids))
        stack.enter_context(mock.patch.object(m, 'lifecycle', self.lifecycle))
        stack.enter_context(mock.patch.object(m, 'stable_snapshot', return_value=self.new))
        stack.enter_context(mock.patch.object(m, 'merge_indexes', return_value=self.report, side_effect=merge_error))
        return stack

    def test_success_quits_and_reopens_once_then_is_idempotent(self):
        with self.contexts():
            self.service.reconcile(self.new)
            self.service.reconcile(self.new)
        self.assertEqual(self.actions, ['quit', 'open'])
        self.assertEqual(self.service.state['completed_handoffs'], 1)
        self.assertNotIn('pending', self.service.state)

    def test_merge_failure_restores_app_and_never_requits_even_after_service_restart(self):
        with self.contexts(m.Deferred('incomplete index')):
            with self.assertRaises(m.Deferred):
                self.service.reconcile(self.new)
            self.service.state['last_error'] = 'incomplete index'
            self.service.save()
            self.service.reconcile(self.new)
            self.service = m.Service()
            self.service.reconcile(self.new)
        self.assertEqual(self.actions, ['quit', 'open'])
        self.assertTrue(self.running)
        self.assertIn('last_error', self.service.state)
        # The next natural exit may finish syncing, but must remain an exit.
        self.running = False
        with self.contexts():
            self.service.reconcile(self.new)
        self.assertFalse(self.running)
        self.assertEqual(self.actions, ['quit', 'open'])
        self.assertNotIn('pending', self.service.state)
        self.assertNotIn('last_error', self.service.state)

    def test_failed_quit_request_is_not_repeated(self):
        with self.contexts(), mock.patch.object(m, 'lifecycle', side_effect=m.Deferred('declined')) as action:
            with self.assertRaises(m.Deferred):
                self.service.reconcile(self.new)
            self.service.reconcile(self.new)
            self.assertEqual(action.call_args_list, [mock.call('quit')])
        self.assertTrue(self.running)

    def test_unexpected_merge_failure_also_restores_app_and_prevents_repeated_quit(self):
        with self.contexts(TypeError('unsupported schema shape')):
            with self.assertRaises(TypeError):
                self.service.reconcile(self.new)
            self.service.reconcile(self.new)
        self.assertEqual(self.actions, ['quit', 'open'])
        self.assertTrue(self.running)

    def test_upgrade_of_failed_old_state_does_not_quit_again(self):
        self.service.state.update({'last_error': 'Conflicting transcript IDs',
                                  'pending': {'outgoing': 'subscription', 'target': 'third_party',
                                              'reopen': True, 'phase': 'waiting_for_exit'}})
        self.service.save()
        self.service = m.Service()
        with self.contexts():
            self.service.reconcile(self.new)
        self.assertEqual(self.actions, [])
        self.assertTrue(self.service.state['pending']['quit_requested'])

    def test_switching_back_cancels_stale_pending_even_when_snapshot_is_identical(self):
        self.service.state['pending'] = {'target': 'third_party', 'reopen': True}
        self.service.state['last_error'] = 'previous failure'
        with self.contexts():
            self.service.reconcile(self.old)
        self.assertEqual(self.actions, [])
        self.assertNotIn('pending', self.service.state)
        self.assertNotIn('last_error', self.service.state)

    def test_initially_closed_app_is_neither_launched_nor_closed_on_a_later_retry(self):
        self.running = False
        with self.contexts(m.Deferred('incomplete index')):
            with self.assertRaises(m.Deferred):
                self.service.reconcile(self.new)
            self.running = True
            self.service.reconcile(self.new)
        self.assertEqual(self.actions, [])

    def test_destination_change_during_quit_restores_app_without_merging(self):
        with self.contexts(), mock.patch.object(m, 'stable_snapshot', return_value=self.old), \
                mock.patch.object(m, 'merge_indexes') as merge:
            with self.assertRaises(m.Deferred):
                self.service.reconcile(self.new)
            merge.assert_not_called()
        self.assertEqual(self.actions, ['quit', 'open'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
