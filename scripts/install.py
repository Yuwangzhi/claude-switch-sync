#!/usr/bin/env python3
"""Install the per-user macOS service without copying private runtime data."""
import argparse
import datetime
import importlib.util
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEST = pathlib.Path.home() / '.local/share/claude-switch-sync'
LABEL = 'com.yuwangzhi.claude-switch-sync'
PLIST = pathlib.Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
DOMAIN = 'gui/' + str(os.getuid())


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def loaded():
    return subprocess.run(['/bin/launchctl', 'print', DOMAIN + '/' + LABEL],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def stop():
    if loaded():
        run(['/bin/launchctl', 'bootout', DOMAIN + '/' + LABEL])


def make_backup():
    backup = DEST / 'deployment-backup' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True, mode=0o700)
    for name in ['switch_sync.py', 'test_switch_sync.py', 'AppLifecycle.swift',
                 'app-lifecycle', 'README.md', 'config.json', 'state.json']:
        source = DEST / name
        if source.is_file():
            shutil.copy2(source, backup / name)
    if PLIST.is_file():
        shutil.copy2(PLIST, backup / PLIST.name)
    return backup


def preflight():
    if sys.platform != 'darwin':
        raise RuntimeError('This LaunchAgent requires macOS.')
    if sys.version_info < (3, 9):
        raise RuntimeError('Python 3.9 or later is required.')
    if DEST.is_symlink() or PLIST.is_symlink():
        raise RuntimeError('Refusing an installation directory or LaunchAgent that is a symlink.')
    if not pathlib.Path('/Applications/Claude.app').is_dir():
        raise RuntimeError('Install Claude Desktop at /Applications/Claude.app first.')
    compiler = run(['/usr/bin/xcrun', '--find', 'swiftc'])
    sdk = run(['/usr/bin/xcrun', '--sdk', 'macosx', '--show-sdk-path'])
    spec = importlib.util.spec_from_file_location('switch_sync', ROOT / 'switch_sync.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = module.stable_snapshot()
    indexes = {}
    for kind, profile in [('subscription', 'Claude'), ('third_party', 'Claude-3p')]:
        root = pathlib.Path.home() / 'Library/Application Support' / profile / 'claude-code-sessions'
        index = module.discover_index(root)
        module.read_index(index)
        indexes[kind] = str(index)
    config = {'version': 1, 'indexes': indexes}
    if (DEST / 'config.json').is_file():
        existing = module.load_json(DEST / 'config.json')
        if existing.get('indexes') != indexes:
            raise RuntimeError('Existing account directories differ. Review them before upgrading.')
    if (DEST / 'state.json').is_file():
        existing_state = module.load_json(DEST / 'state.json')
        if existing_state.get('version') != 1 or 'observed' not in existing_state:
            raise RuntimeError('Existing service state has an unsupported format; it was not replaced.')
    return module, compiler, sdk, current, config


def install(check_only=False):
    module, compiler, sdk, current, config = preflight()
    if check_only:
        print('Preflight passed: compiler, CC Switch state and both session indexes are available.')
        print('No service or session data was changed.')
        return
    # Finish compiling before stopping an existing working service.
    with tempfile.TemporaryDirectory(prefix='claude-switch-sync-build-') as temp:
        helper = pathlib.Path(temp) / 'app-lifecycle'
        run([compiler, '-sdk', sdk, '-O', str(ROOT / 'AppLifecycle.swift'),
             '-o', str(helper), '-framework', 'AppKit'])
        was_loaded = loaded()
        backup = make_backup()
        stop()
        try:
            for name in ['switch_sync.py', 'test_switch_sync.py', 'AppLifecycle.swift', 'README.md']:
                module.atomic_write(DEST / name, (ROOT / name).read_bytes())
            module.atomic_write(DEST / 'app-lifecycle', helper.read_bytes())
            (DEST / 'app-lifecycle').chmod(0o700)
            if not (DEST / 'config.json').exists():
                module.atomic_write(DEST / 'config.json', module.encoded(config))
            if not (DEST / 'state.json').exists():
                module.atomic_write(DEST / 'state.json', module.encoded({
                    'version': 1, 'observed': current, 'completed_handoffs': 0}))
            plist = {
                'Label': LABEL,
                'ProgramArguments': [sys.executable, '-u', str(DEST / 'switch_sync.py'), 'watch'],
                'WorkingDirectory': str(DEST),
                'RunAtLoad': True,
                'KeepAlive': True,
                'ProcessType': 'Background',
                'ThrottleInterval': 5,
                'StandardOutPath': str(DEST / 'events.jsonl'),
                'StandardErrorPath': str(DEST / 'errors.log'),
            }
            module.atomic_write(PLIST, plistlib.dumps(plist))
            run(['/bin/launchctl', 'bootstrap', DOMAIN, str(PLIST)])
        except Exception:
            # Restore only deployment artifacts; never rewind session indexes,
            # provider credentials or state a running service may have updated.
            stop()
            for name in ['switch_sync.py', 'test_switch_sync.py', 'AppLifecycle.swift',
                         'app-lifecycle', 'README.md']:
                if (backup / name).is_file():
                    shutil.copy2(backup / name, DEST / name)
            if (backup / PLIST.name).is_file():
                shutil.copy2(backup / PLIST.name, PLIST)
                if was_loaded:
                    run(['/bin/launchctl', 'bootstrap', DOMAIN, str(PLIST)])
            raise
    print('Installed and started:', DEST)
    print('Previous deployment backup:', backup)
    print('Session transcripts, credentials and provider settings were not modified by the installer.')


def uninstall():
    if sys.platform != 'darwin':
        raise RuntimeError('This LaunchAgent requires macOS.')
    if DEST.is_symlink() or PLIST.is_symlink():
        raise RuntimeError('Refusing a symlinked installation or LaunchAgent.')
    stop()
    if PLIST.exists():
        backup = make_backup()
        shutil.move(str(PLIST), str(backup / ('disabled-' + PLIST.name)))
        print('Login startup removed. LaunchAgent saved in:', backup)
    else:
        print('No login startup file is installed.')
    print('Program files, local backups, logs and all chat data were kept.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--check', action='store_true', help='Read-only prerequisite check')
    flags.add_argument('--uninstall', action='store_true', help='Remove login startup; keep all local data')
    args = parser.parse_args()
    try:
        uninstall() if args.uninstall else install(args.check)
    except Exception as error:
        print('Installation stopped:', error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
