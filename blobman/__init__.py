import datetime
from getpass import getpass
import glob
import itertools
import json
import os
from pathlib import Path
import random
from attr import field
import cattrs
import click
from invoke import run
from attrs import define
import pendulum


def _git_root(start=os.getcwd()):
    p = Path(start)
    while not (p / '.git').is_dir():
        if p == p.root:
            return None
        p = p.parent
    return p


GIT_ROOT = Path(_git_root())
GITIGNORE_PATH = GIT_ROOT / '.gitignore'
BLOBMAN_PATH = GIT_ROOT / Path('.blobman/')
CONFIG_PATH = BLOBMAN_PATH / 'config.json'
LOCK_PATH = BLOBMAN_PATH / 'lock.json'
LIST_PATH = BLOBMAN_PATH / 'list.txt'
PASSWORD_PATH = BLOBMAN_PATH / 'password.txt'


@click.group(help='A simple large binary file manager alongside git, based on Restic backup tool.')
def cli():
    ...


@cli.command(help='initialize blobman config files & restic repository')
def init():
    if not _git_root():
        print('Error: not a git repository')
        return
    if BLOBMAN_PATH.exists():
        print('Error: blobman repository already initialized')
        return

    c = BlobmanConfig(
        repository_url=input('Restic Repository: '),
        repository_password=getpass('Restic Password: '),
    )

    BLOBMAN_PATH.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.touch(exist_ok=True)
    LOCK_PATH.touch(exist_ok=True)
    PASSWORD_PATH.touch(exist_ok=True)

    GITIGNORE_PATH.touch(exist_ok=True)

    run('restic init', hide=True, env=_get_env(c))
    run(f'git add {LOCK_PATH} {CONFIG_PATH}', hide=True)

    _ensure_line('.blobman/password.txt', GITIGNORE_PATH)
    _store_config(c)


@cli.command(help='snapshot local blob state remotely')
@click.option('--dry', 'dry', is_flag=True, default=False, help='show what will be snapshoted')
def snapshot(dry):
    c = _load_config()
    _ensure_no_collisions(c)
    diff = _diff_locks(c.lock, c.worktree_lock)
    if dry:
        _print_diff(diff)
        return
    files = [f['file'] for f in c.worktree_lock.tracked_files]

    # save worktree lock upfront
    env = _get_env(c)
    snaps = json.loads(run('restic snapshots --json', hide=True, env=env).stdout)
    tags = set(t for s in snaps for t in s.get('tags', ()) if t.startswith('blobmanid:'))
    while ('blobmanid:' + (id := hex(random.getrandbits(32))[2:])) in tags:
        pass
    c.lock = c.worktree_lock  # (same object now)
    c.lock.snapshot_tag = id
    _store_config(c)  # TODO: not assume it won't break... store and later do atomic move...

    LIST_PATH.write_text('\n'.join(files + ['.blobman/lock.json']))
    run(f'restic backup --tag blobmanid:{id} --files-from-verbatim {str(LIST_PATH)}', env=env)
    os.remove(LIST_PATH)


@cli.command(help='sync local state with expected state in lockfile')
@click.argument('target', required=False)
@click.option('--dry', 'dry', is_flag=True, default=False)
def checkout(target, dry):
    c = _load_config()
    if not target:
        target = c.lock.snapshot_tag

    env = _get_env(c)
    snaps = json.loads(run('restic snapshots --json', env=env, hide=True).stdout)
    snapshot_id = next(s['short_id'] for s in snaps if f'blobmanid:{target}' in s['tags'])

    target_lock = None
    if target == c.lock.snapshot_tag:
        target_lock = c.lock
    else:
        res = run(f'restic dump {snapshot_id} .blobman/lock.json', env=env, hide=True)
        target_lock = cattrs.structure(json.loads(res.stdout), LockInfo)

    diff = _diff_locks(c.worktree_lock, target_lock)
    if dry:
        _print_diff(diff)
        return
    run(f'restic restore {snapshot_id} --target "{c.git_root}"', env=env, hide=True)


@cli.command(help='list exact files & snapshot history')
def status():
    c = _load_config()

    print('Patterns:\n')
    for id, pat in c.include_patterns.items():
        print(id, '-', pat)

    print('\nTracked blobs:\n')
    for p in _ls(c.include_patterns, root_dir=_git_root()):
        print(p)

    print('\nSnapshots:\n')
    snapshots = json.loads(run('restic snapshots --json', hide=True, env=_get_env(c)).stdout)
    by_date = lambda s: datetime.datetime.fromisoformat(s['time'])
    for s in sorted(snapshots, reverse=True, key=by_date):
        id = next(s.removeprefix('blobmanid:') for s in s['tags'] if s.startswith('blobmanid:'))
        now, then = pendulum.now(), pendulum.parse(s['time'])
        print(
            "{} {} | {} ({})".format(
                id,
                "*" if id == c.worktree_lock.snapshot_tag else " ",
                (now - then).in_words() + ' ago',
                then.to_day_datetime_string(),
            )
        )


@cli.command(help='track glob pattern')
@click.argument('pattern')
def add(pattern):
    c = _load_config()
    if pattern in c.include_patterns.values():
        return
    while (id := hex(random.getrandbits(32))[2:]) in c.include_patterns:
        pass
    c.include_patterns[id] = pattern
    _ensure_no_collisions(c)
    print(f'{id} - {pattern}')
    _store_config(c)


@cli.command(help='remove tracked pattern by its ID')
@click.argument('pattern_id')
def remove(pattern_id: str):
    c = _load_config()
    del c.include_patterns[pattern_id]
    _store_config(c)


@define(kw_only=True)
class LockInfo:
    snapshot_tag: str | None = None
    tracked_files: list[dict[str, str]] = field(factory=list)


@define(kw_only=True)
class BlobmanConfig:
    git_root: Path = _git_root()
    repository_url: str
    repository_password: str
    include_patterns: dict[str, str] = field(factory=dict)
    lock: LockInfo = field(factory=LockInfo)
    worktree_lock: LockInfo = field(factory=LockInfo)


@define(kw_only=True)
class LockDiff:
    added_files: list[dict[str, str]] = field(factory=list)
    removed_files: list[dict[str, str]] = field(factory=list)
    modified_files: list[dict[str, str]] = field(factory=list)
    unchanged_files: list[dict[str, str]] = field(factory=list)


def _load_config() -> BlobmanConfig:
    for p in (CONFIG_PATH, LOCK_PATH,):
        if not p.exists():
            print(f'Error: file not found {p}')
            exit(1)
    config_json = json.loads(CONFIG_PATH.read_text())
    lock = json.loads(LOCK_PATH.read_text())
    worktree_lock = {
        'tracked_files': [
            {
                'file': f,
                'hash': run(f'shasum {f} | awk "{{ print $1 }}"', hide=True).stdout.strip()
            } for f in _ls(config_json['include_patterns'])
        ]
    }
    lock_map = {f['file']: f['hash'] for f in lock['tracked_files']}
    worktree_map = {f['file']: f['hash'] for f in worktree_lock['tracked_files']}
    if lock_map == worktree_map:
        worktree_lock['snapshot_tag'] = lock['snapshot_tag']
    else:
        worktree_lock['snapshot_tag'] = None
    return cattrs.structure(
        config_json | {
            'git_root': _git_root(),
            'repository_password': PASSWORD_PATH.read_text(),
            'lock': lock,
            'worktree_lock': worktree_lock,
        },
        BlobmanConfig
    )


def _store_config(config: BlobmanConfig) -> None:
    o = cattrs.unstructure(config)
    o.pop('git_root')
    PASSWORD_PATH.write_text(o.pop('repository_password'))
    LOCK_PATH.write_text(json.dumps(o.pop('lock'), indent=4))
    o.pop('worktree_lock')
    CONFIG_PATH.write_text(json.dumps(o, indent=4))


def _get_env(c: BlobmanConfig):
    return {
        'PWD': _git_root(),
        'RESTIC_REPOSITORY': c.repository_url,
        'RESTIC_PASSWORD': c.repository_password,
    }


def _ls(patterns: dict[str, str], root_dir=None):
    fn = lambda p: glob.glob(p, recursive=True, include_hidden=False)
    paths = set(itertools.chain(*map(fn, patterns.values())))
    for p in set(paths):
        if Path(p).is_dir():
            paths |= set(glob.glob(str(Path(p) / '**'), recursive=True, include_hidden=False))
    for p in set(paths):
        if not Path(p).is_file():
            paths.discard(p)  # discard non-regular-files
    for p in sorted(paths):
        yield p


# ensure no collisions between tracked files and git files
def _ensure_no_collisions(c: BlobmanConfig):
    blob_matching = set(_ls(c.include_patterns))
    git_staged = set(run("git diff --name-only --cached", hide=True).stdout.split('\n'))
    git_tracked = set(run('git ls-tree -r HEAD --name-only', hide=True).stdout.split('\n'))
    common = blob_matching.intersection(git_staged) | blob_matching.intersection(git_tracked)
    if common:
        print('Error: collision between git tracked files and blobs\n')
        print('\nFollowing files are tracked both by .git and blobman:\n')
        print('\n'.join(common))
        print('\nIn order to fix a problem, you must correct following patterns:\n')
        print('... TODO')
        exit(1)


def _ensure_line(line, file):
    if line in map(str.strip, Path(file).read_text().split('\n')):
        return
    with Path(file).open('a') as f:
        f.write(str(line) + '\n')


def _diff_locks(src: LockInfo, dst: LockInfo) -> LockDiff:
    if src.snapshot_tag == dst.snapshot_tag and None not in (src.snapshot_tag, dst.snapshot_tag):
        return LockDiff(
            unchanged_files=set(p['file'] for p in src.tracked_files)
        )
    src_files = set(p['file'] for p in src.tracked_files)
    dst_files = set(p['file'] for p in dst.tracked_files)
    common_files = src_files.intersection(dst_files)
    src_map = {f['file']: f['hash'] for f in src.tracked_files}
    dst_map = {f['file']: f['hash'] for f in dst.tracked_files}
    changed_files = set(p for p in common_files if src_map[p] != dst_map[p])
    return LockDiff(
        added_files=dst_files - src_files,
        removed_files=src_files - dst_files,
        modified_files=changed_files,
        unchanged_files=common_files - changed_files,
    )


def _print_diff(diff: LockDiff) -> None:
    print('\nUnchanged files:\n')
    print('\n'.join(diff.unchanged_files))
    print('\nModified files:\n')
    print('\n'.join(diff.modified_files))
    print('\nAdded files:\n')
    print('\n'.join(diff.added_files))
    print('\nRemoved files:\n')
    print('\n'.join(diff.removed_files))
