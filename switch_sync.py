#!/usr/bin/python3
"""Event-triggered Claude subscription/third-party session-index handoff.

No transcript, credential, provider, or user project is modified. No periodic
history copying. Desktop metadata is written only after Claude has exited.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

BASE = pathlib.Path(__file__).resolve().parent
HOME = pathlib.Path.home()
APP_EXE = '/Applications/Claude.app/Contents/MacOS/Claude'
VALID_KINDS = {'subscription', 'third_party'}
PROFILE_SELECTION_FIELDS = ('model', 'effort')


class Deferred(Exception):
    pass


def load_json(path):
    return json.loads(path.read_bytes())


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def emit(event, **fields):
    row = {'time': dt.datetime.now().astimezone().isoformat(), 'event': event, **fields}
    print(json.dumps(row, ensure_ascii=False), flush=True)


def app_pids():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, check=True)
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts := line.strip().split(None, 1)) == 2 and parts[1] == APP_EXE]


def require_stopped():
    if app_pids():
        raise Deferred('Claude Desktop is running; session indexes were not overwritten')


def kind_of_provider(provider):
    env = provider.get('settings', {}).get('env', {})
    auth = any(env.get(k) for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'))
    endpoint = env.get('ANTHROPIC_BASE_URL', '')
    host = urlsplit(endpoint).hostname if endpoint else None
    if not auth and host in (None, 'api.anthropic.com') and (
            provider.get('category') == 'official' or provider.get('id') == 'default'):
        return 'subscription'
    return 'third_party'


def connection_mode(live):
    mode = live.get('mode', 'direct')
    if mode in ('stack', 'aggregation') or (mode == 'proxy' and live.get('stack', {}).get('enabled')):
        return 'aggregation'
    return 'routing' if mode == 'proxy' else 'direct'


def crosses_boundary(previous, current):
    return previous.get('kind') in VALID_KINDS and current.get('kind') in VALID_KINDS and previous['kind'] != current['kind']


def snapshot(home=HOME):
    settings = load_json(home / '.cc-switch/settings.json')
    live = load_json(home / '.cc-switch/live-state.json').get('apps', {})
    with sqlite3.connect('file:' + str(home / '.cc-switch/cc-switch.db') + '?mode=ro', uri=True, timeout=2) as db:
        providers = {}
        for pid, app, category, raw in db.execute(
                "SELECT id,app_type,category,settings_config FROM providers WHERE app_type IN ('claude','claude-desktop')"):
            providers[(app, pid)] = {'id': pid, 'category': category, 'settings': json.loads(raw or '{}')}
    result = {}
    for app, key, short in [('claude', 'currentProviderClaude', 'cli'),
                            ('claude-desktop', 'currentProviderClaudeDesktop', 'desktop')]:
        active = live.get(app, {})
        mode = connection_mode(active)
        pid = active.get('proxy_route') if mode != 'direct' else settings.get(key)
        provider = providers.get((app, pid))
        if provider is None:
            raise Deferred('Provider selection is not settled: ' + short)
        kind = kind_of_provider(provider) if mode == 'direct' else 'third_party'
        result[short] = {'kind': kind, 'mode': mode, 'provider_id': pid}
    # Config writes and live-state commits are separate operations. Act only
    # when both agree; transient writes and CC Switch shutdown are not switches.
    cli = load_json(home / '.claude/settings.json')
    env = cli.get('env', {})
    external = bool(env.get('ANTHROPIC_API_KEY') or env.get('ANTHROPIC_AUTH_TOKEN') or cli.get('apiKeyHelper'))
    endpoint = env.get('ANTHROPIC_BASE_URL', '')
    if endpoint and urlsplit(endpoint).hostname not in (None, 'api.anthropic.com'):
        external = True
    if external != (result['cli']['kind'] == 'third_party'):
        raise Deferred('CLI configuration update is still in progress')
    for name in ('Claude', 'Claude-3p'):
        config = load_json(home / 'Library/Application Support' / name / 'claude_desktop_config.json')
        mode = config.get('deploymentMode', '1p')
        if mode not in ('1p', '3p') or (mode == '3p') != (result['desktop']['kind'] == 'third_party'):
            raise Deferred('Desktop configuration update is still in progress')
    # CC Switch changes the provider keys, not the CLI project-history root.
    result['cli']['history_root'] = str(home / '.claude/projects')
    return result


def stable_snapshot():
    before = snapshot()
    time.sleep(0.7)
    after = snapshot()
    if before != after:
        raise Deferred('Switch configuration is still changing')
    return after


def discover_index(root):
    candidates = {p.parent for p in root.glob('*/*/local_*.json') if p.is_file() and not p.is_symlink()}
    # Desktop can copy its official-account namespace into Claude-3p on first
    # launch. The local gateway's profile-origin marker identifies its active
    # namespace; a copied official namespace must not be mistaken for it.
    marked = []
    for directory in candidates:
        marker = directory.parent / (directory.name + '.profile-origin.json')
        if marker.is_file() and not marker.is_symlink():
            value = load_json(marker)
            if value.get('mode') == 'local' and value.get('org') == directory.name:
                marked.append(directory)
    if len(marked) == 1:
        return marked[0]
    if len(candidates) != 1:
        raise Deferred('Expected one account index under ' + str(root))
    return next(iter(candidates))


def read_index(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise Deferred('Session index is missing or is a symlink')
    records, files, deleted = {}, {}, {}
    for path in sorted(directory.iterdir()):
        if path.is_symlink():
            raise Deferred('Unexpected symlink in the session index')
        if not path.is_file():
            continue
        if path.name.startswith('local_') and path.suffix == '.json':
            data = path.read_bytes()
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError):
                raise Deferred('A session index record is incomplete; waiting for a stable file')
            sid = value.get('sessionId')
            if not isinstance(sid, str) or sid + '.json' != path.name:
                raise Deferred('Unexpected session ID or filename; no index was written')
            records[sid] = value
            files[path.name] = data
        elif path.name.startswith('deleted_'):
            data = path.read_bytes()
            try:
                deleted[path.name[len('deleted_'):]] = int(data.strip())
            except ValueError:
                raise Deferred('Invalid deletion marker; no index was written')
            files[path.name] = data
        elif path.name == 'archived-sessions.idx':
            files[path.name] = path.read_bytes()
    return records, files, deleted


def record_rank(record):
    def number(key):
        value = record.get(key, 0)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    return number('lastActivityAt'), number('lastTurnEdgeAt'), number('completedTurns')


def has_same_transcript_lineage(chosen, other):
    """Accept a changed CLI head only when Desktop records its predecessor.

    An overlap between two prior-ID lists alone is not enough: those heads may
    be independent branches. Keep both originals when identity is ambiguous.
    """
    if chosen.get('cliSessionId') == other.get('cliSessionId'):
        return True
    prior = chosen.get('priorCliSessionIds')
    created = chosen.get('createdAt')
    return (isinstance(prior, list) and all(isinstance(x, str) for x in prior)
            and isinstance(chosen.get('cliSessionId'), str)
            and bool(chosen['cliSessionId'])
            and isinstance(other.get('cliSessionId'), str)
            and bool(other['cliSessionId']) and other['cliSessionId'] in prior
            and chosen.get('sessionId') == other.get('sessionId')
            and isinstance(created, (int, float)) and not isinstance(created, bool)
            and created > 0 and created == other.get('createdAt')
            and isinstance(chosen.get('cwd'), str) and bool(chosen['cwd'])
            and chosen['cwd'] == other.get('cwd'))


def plan_merge(left, right, outgoing):
    records = {'subscription': left[0], 'third_party': right[0]}
    deleted = {}
    for source in (left[2], right[2]):
        for key, value in source.items():
            deleted[key] = max(deleted.get(key, 0), value)
    desired, conflicts, skipped_deleted, identity_conflicts = {}, [], [], []
    for sid in sorted(records['subscription'].keys() | records['third_party'].keys()):
        a, b = records['subscription'].get(sid), records['third_party'].get(sid)
        if a is None or b is None:
            chosen = a if a is not None else b
        else:
            if a != b:
                conflicts.append(sid)
            ar, br = record_rank(a), record_rank(b)
            chosen = a if ar > br else b if br > ar else records[outgoing][sid]
            other = b if chosen is a else a
            if not has_same_transcript_lineage(chosen, other):
                # A single unrelated branch must not block every other session
                # or prevent Desktop from reopening. Leave this pair untouched.
                identity_conflicts.append(sid)
                continue
        cli_id = chosen.get('cliSessionId')
        if cli_id in deleted and deleted[cli_id] >= record_rank(chosen)[0]:
            # Do not resurrect deleted sessions, and never delete user records.
            skipped_deleted.append(sid)
            continue
        # Keep the selected record byte-for-byte; do not reformat untouched data.
        selected_files = left[1] if chosen is a else right[1]
        desired[sid + '.json'] = selected_files[sid + '.json']
    # Preserve the archive schema, reconcile visibility from the chosen records.
    archive_ids = set()
    for index in (left, right):
        if 'archived-sessions.idx' in index[1]:
            value = json.loads(index[1]['archived-sessions.idx'])
            if value.get('v') != 1 or not isinstance(value.get('archived'), list) or not all(isinstance(x, str) for x in value['archived']):
                raise Deferred('Unsupported archive-index schema')
            archive_ids.update(value['archived'])
    for filename, data in desired.items():
        value = json.loads(data)
        if value.get('isArchived'):
            archive_ids.add(value['sessionId'])
        else:
            archive_ids.discard(value['sessionId'])
    desired['archived-sessions.idx'] = encoded({'v': 1, 'archived': sorted(archive_ids)})
    return desired, conflicts, skipped_deleted, identity_conflicts


def with_profile_selection(data, existing):
    """Combine shared progress with this profile's own model/effort choice.

    Field absence is meaningful: it delegates the choice to Desktop's defaults.
    A first import therefore uses the destination's defaults instead of a model
    alias or effort level that belongs to the other provider.
    """
    record = json.loads(data)
    adjusted = dict(record)
    for key in PROFILE_SELECTION_FIELDS:
        if existing is not None and key in existing:
            adjusted[key] = existing[key]
        else:
            adjusted.pop(key, None)
    return data if adjusted == record else encoded(adjusted)


def merge_indexes(config, outgoing, guard=require_stopped, backup_root=None):
    guard()
    directories = {key: pathlib.Path(config['indexes'][key]) for key in VALID_KINDS}
    first = {key: read_index(path) for key, path in directories.items()}
    time.sleep(0.15)
    guard()
    second = {key: read_index(path) for key, path in directories.items()}
    if first != second:
        raise Deferred('Session metadata is still being written')
    desired, conflicts, deleted, identity_conflicts = plan_merge(second['subscription'], second['third_party'], outgoing)
    per_kind = {}
    selection_overlays = 0
    for kind in directories:
        per_kind[kind] = dict(desired)
        for name, data in desired.items():
            if not name.startswith('local_'):
                continue
            existing = second[kind][0].get(name[:-len('.json')])
            adjusted = with_profile_selection(data, existing)
            selection_overlays += adjusted != data
            if existing is not None and json.loads(adjusted) == existing:
                # Preserve the destination's exact formatting on a no-op pass.
                adjusted = second[kind][1][name]
            per_kind[kind][name] = adjusted
        archive_ids = set(json.loads(desired['archived-sessions.idx'])['archived'])
        for sid in identity_conflicts:
            # An unresolved pair retains each side's own archive visibility.
            if second[kind][0][sid].get('isArchived'):
                archive_ids.add(sid)
            else:
                archive_ids.discard(sid)
        per_kind[kind]['archived-sessions.idx'] = encoded({'v': 1, 'archived': sorted(archive_ids)})
    event_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    backup = (pathlib.Path(backup_root) if backup_root else BASE / 'backups') / event_id
    backup.mkdir(parents=True, mode=0o700)
    for kind, (_, files, _) in second.items():
        for name, data in files.items():
            atomic_write(backup / kind / name, data)
    # Plan and back up both sides before the first mutation. Never concatenate
    # transcripts or copy settings/credentials into a different profile.
    writes = 0
    for kind, directory in directories.items():
        old = second[kind][1]
        for name, data in per_kind[kind].items():
            if old.get(name) == data:
                continue
            guard()
            target = directory / name
            current = target.read_bytes() if target.exists() else None
            if current != old.get(name):
                raise Deferred('Concurrent index change detected; remaining writes deferred, backup=' + event_id)
            atomic_write(target, data)
            writes += 1
    guard()
    for kind, directory in directories.items():
        for name, data in per_kind[kind].items():
            if (directory / name).read_bytes() != data:
                raise Deferred('Session index verification failed, backup=' + event_id)
    report = {'event_id': event_id, 'session_count': len(desired) - 1, 'writes': writes,
              'conflict_count': len(conflicts), 'skipped_deleted_count': len(deleted),
              'unresolved_identity_count': len(identity_conflicts),
              'profile_selection_overlays': selection_overlays,
              'transcripts_modified': 0, 'files_deleted': 0}
    atomic_write(backup / 'report.json', encoded(report))
    if identity_conflicts:
        atomic_write(backup / 'unresolved-session-ids.json', encoded(identity_conflicts))
    return report


def lifecycle(command):
    result = subprocess.run([str(BASE / 'app-lifecycle'), command], capture_output=True, text=True, timeout=12)
    if result.returncode:
        raise Deferred('Claude lifecycle request failed: ' + command)


class Service:
    def __init__(self):
        self.config = load_json(BASE / 'config.json')
        self.path = BASE / 'state.json'
        self.state = load_json(self.path)
        # Upgrade an already-failed v1 handoff without issuing another quit.
        pending = self.state.get('pending')
        if pending and 'quit_requested' not in pending and self.state.get('last_error'):
            pending['quit_requested'] = True
            pending['phase'] = 'waiting_for_manual_exit'
            self.save()

    def save(self):
        atomic_write(self.path, encoded(self.state))

    def reconcile(self, now):
        previous = self.state['observed']
        if crosses_boundary(previous['cli'], now['cli']):
            root = pathlib.Path(now['cli']['history_root'])
            if previous['cli']['history_root'] != str(root) or not root.is_dir():
                raise Deferred('CLI history root changed; automatic relocation is not allowed')
            emit('cli_history_shared', from_kind=previous['cli']['kind'], to_kind=now['cli']['kind'],
                 from_mode=previous['cli']['mode'], to_mode=now['cli']['mode'],
                 transcript_files=sum(1 for _ in root.glob('*/*.jsonl')), files_modified=0)
        if not crosses_boundary(previous['desktop'], now['desktop']):
            if previous != now or self.state.get('pending'):
                emit('no_history_migration', cli_mode=now['cli']['mode'], desktop_mode=now['desktop']['mode'])
                self.state['observed'] = now
                self.state.pop('pending', None)
                self.state.pop('last_error', None)
                self.save()
            return
        pending = self.state.get('pending')
        if not pending or pending['target'] != now['desktop']['kind']:
            pending = {'outgoing': previous['desktop']['kind'], 'target': now['desktop']['kind'],
                       'reopen': bool(app_pids()), 'phase': 'waiting_for_exit', 'quit_requested': False}
            self.state['pending'] = pending
            self.save()
            emit('desktop_switch_detected', from_kind=pending['outgoing'], to_kind=pending['target'],
                 from_mode=previous['desktop']['mode'], to_mode=now['desktop']['mode'])
        try:
            if app_pids():
                if pending.get('quit_requested') or not pending['reopen']:
                    if pending['phase'] != 'waiting_for_manual_exit':
                        pending['phase'] = 'waiting_for_manual_exit'
                        self.save()
                    return
                # Persist before sending the request, including across daemon
                # restarts: one switch can never become a repeated-quit loop.
                pending['quit_requested'] = True
                self.save()
                emit('desktop_quit_requested', target=pending['target'])
                lifecycle('quit')
                deadline = time.monotonic() + 20
                while app_pids() and time.monotonic() < deadline:
                    time.sleep(0.25)
                require_stopped()
            # Recheck the destination after quitting: the user may switch again.
            fresh = stable_snapshot()
            if fresh['desktop']['kind'] != pending['target']:
                raise Deferred('Destination changed while Claude was quitting')
            report = merge_indexes(self.config, pending['outgoing'])
        except Exception:
            pending['phase'] = 'waiting_for_manual_exit'
            restore = pending['reopen'] and pending.get('quit_requested')
            # Once restored, a later natural exit must not reopen the app again.
            pending['reopen'] = False
            self.save()
            if restore and not app_pids():
                lifecycle('open')
                emit('desktop_restored_after_deferred_handoff', target=pending['target'])
            raise
        self.state['observed'] = fresh
        self.state['completed_handoffs'] = self.state.get('completed_handoffs', 0) + 1
        self.state['last_handoff'] = report
        self.state.pop('last_error', None)
        if report['unresolved_identity_count']:
            self.state['last_warning'] = 'Unrelated transcript heads were kept separately; see the handoff backup'
        else:
            self.state.pop('last_warning', None)
        pending['phase'] = 'merged'
        self.save()
        if pending['reopen']:
            lifecycle('open')
        self.state.pop('pending', None)
        self.save()
        emit('desktop_handoff_complete', **report, reopened=pending['reopen'])


class Watcher:
    def __init__(self):
        self.queue = select.kqueue()
        self.fds = []
        self.pid_events = set()

    def arm(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []
        files = [HOME / '.cc-switch/settings.json', HOME / '.cc-switch/live-state.json',
                 HOME / '.claude/settings.json',
                 HOME / 'Library/Application Support/Claude/claude_desktop_config.json',
                 HOME / 'Library/Application Support/Claude-3p/claude_desktop_config.json']
        events = []
        for path in set(files + [p.parent for p in files]):
            if not path.exists():
                continue
            fd = os.open(str(path), getattr(os, 'O_EVTONLY', os.O_RDONLY))
            self.fds.append(fd)
            events.append(select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                        fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE))
        for pid in app_pids():
            if pid not in self.pid_events:
                events.append(select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                            fflags=select.KQ_NOTE_EXIT))
                self.pid_events.add(pid)
        for event in events:
            try:
                self.queue.control([event], 0, 0)
            except ProcessLookupError:
                pass

    def wait(self):
        self.queue.control(None, 32, None)
        # Coalesce the several atomic writes made by one CC Switch action.
        started = time.monotonic()
        while time.monotonic() - started < 3:
            if not self.queue.control(None, 32, 0.5):
                break


def initialize():
    roots = {}
    for kind, name in [('subscription', 'Claude'), ('third_party', 'Claude-3p')]:
        roots[kind] = str(discover_index(HOME / 'Library/Application Support' / name / 'claude-code-sessions'))
    atomic_write(BASE / 'config.json', encoded({'version': 1, 'indexes': roots}))
    if not (BASE / 'state.json').exists():
        atomic_write(BASE / 'state.json', encoded({'version': 1, 'observed': stable_snapshot(), 'completed_handoffs': 0}))
    emit('initialized', history_writes=0, periodic_sync=False)


def daemon():
    with (BASE / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        service, watcher = Service(), Watcher()
        emit('watcher_started', pid=os.getpid(), periodic_sync=False)
        while True:
            watcher.arm()
            try:
                if service.state.get('pending', {}).get('phase') == 'merged':
                    if service.state['pending']['reopen'] and not app_pids():
                        lifecycle('open')
                    service.state.pop('pending', None)
                    service.save()
                service.reconcile(stable_snapshot())
                if 'last_error' in service.state and not service.state.get('pending'):
                    service.state.pop('last_error', None)
                    service.save()
            except Exception as error:
                message = str(error)
                if service.state.get('last_error') != message:
                    emit('deferred', reason=message)
                    service.state['last_error'] = message
                    service.save()
            watcher.arm()
            watcher.wait()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['init', 'watch', 'status'])
    args = parser.parse_args()
    if args.command == 'init':
        initialize()
    elif args.command == 'status':
        print(json.dumps(load_json(BASE / 'state.json'), ensure_ascii=False, indent=2))
    else:
        daemon()


if __name__ == '__main__':
    main()
