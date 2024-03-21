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
VCS_LOCK_PATH = BLOBMAN_PATH / 'lock.json'
WORKTREE_LOCK_PATH = BLOBMAN_PATH / 'worktree-lock.json'
LIST_PATH = BLOBMAN_PATH / 'list.txt'
PASSWORD_PATH = BLOBMAN_PATH / 'password.txt'


@click.group
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
    WORKTREE_LOCK_PATH.touch(exist_ok=True)
    VCS_LOCK_PATH.touch(exist_ok=True)
    PASSWORD_PATH.touch(exist_ok=True)

    GITIGNORE_PATH.touch(exist_ok=True)
    _ensure_line('.blobman/password.txt', GITIGNORE_PATH)
    _ensure_line('.blobman/worktree-lock.json', GITIGNORE_PATH)
    _store_config(c)

    run('restic init', env=_get_env(c))
    run(f'git add {WORKTREE_LOCK_PATH} {CONFIG_PATH}')


@cli.command(help='take a snapshot of local blobs')
@click.option('--dry', 'dry', is_flag=True, default=False, help='show what will be snapshoted')
def snapshot(dry):
    c = _load_config()
    env = _get_env(c)
    files = list(_ls(c))
    if dry:
        old_locked_files = [p['file'] for p in c.lock.tracked_files]
        print('\nTracked files:\n')
        new_files = []
        for f in files:
            print(f)
            if f not in old_locked_files:
                new_files.append(f)
        
        print('\nNew files:\n')
        for f in new_files:
            print(f)

        print('\nRemoved files:\n')
        for f in old_locked_files:
            if f not in files:
                print()
        return
    c.lock = LockInfo(
        tracked_files=[{'file': f} for f in files]
    )
    LIST_PATH.write_text(os.linesep.join(files))
    res = run(f'restic backup --files-from-verbatim {str(LIST_PATH)}', env=env)
    os.remove(LIST_PATH)
    c.snapshot_id = res.stdout.split('\n')[-2].split(' ')[1]
    _store_config(c)
    _store_lock(c)


@cli.command(help='ensure latest blobs are present')
def checkout():
    c = _load_config()
    env = _get_env(c)
    run(f'restic restore {c.snapshot_id} --target "{os.getcwd()}"', env=env)


@cli.command(help='list exact files & snapshot history')
@click.option('--diff', is_flag=True, default=False, help='show diff of local & remote blobs')
def status(diff):
    c = _load_config()

    print('Patterns:')
    print()
    for id, pat in c.include_patterns.items():
        print(id, pat)

    print()
    print('Tracked blobs:')
    print()
    for p in _ls(c, root_dir=_git_root()):
        print(p)

    print()
    print('Snapshots:')
    run('restic snapshots', env=_get_env(c))
    print()


@cli.command(help='track glob pattern')
@click.argument('pattern', required=False)
def add(pattern):
    c = _load_config()
    if pattern in c.include_patterns.values():
        return
    while (id := hex(random.getrandbits(28))[2:]) in c.include_patterns:
        pass
    c.include_patterns[id] = pattern
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
    snapshot_id: str | None = None
    tracked_files: list[dict[str, str]] = field(factory=list)


@define(kw_only=True)
class BlobmanConfig:
    git_root: Path = _git_root()
    repository_url: str
    repository_password: str
    include_patterns: dict[str, str] = field(factory=dict)
    vcs_lock: LockInfo = field(factory=LockInfo)
    worktree_lock: LockInfo = field(factory=LockInfo)


def _load_config() -> BlobmanConfig:
    for p in (CONFIG_PATH, VCS_LOCK_PATH, WORKTREE_LOCK_PATH):
        if not p.exists():
            print(f'Error: file not found {p}')
            exit(1)
    return cattrs.structure(
        json.loads(CONFIG_PATH.read_text()) | {
            'git_root': _git_root(),
            'repository_password': PASSWORD_PATH.read_text(),
            'lock': json.loads(VCS_LOCK_PATH.read_text()),
            'worktree_lock': json.loads(WORKTREE_LOCK_PATH.read_text())
        },
        BlobmanConfig
    )


def _store_config(config: BlobmanConfig) -> None:
    o = cattrs.unstructure(config)
    PASSWORD_PATH.write_text(json.dumps(o.pop('repository_password'), indent=4))
    VCS_LOCK_PATH.write_text(json.dumps(o.pop('vcs_lock'), indent=4))
    WORKTREE_LOCK_PATH.write_text(json.dumps(o.pop('worktree_lock'), indent=4))
    CONFIG_PATH.write_text(json.dumps(o, indent=4))


def _get_env(c: BlobmanConfig):
    return {
        'PWD': _git_root(),
        'RESTIC_REPOSITORY': c.repository_url,
        'RESTIC_PASSWORD': PASSWORD_PATH.read_text().strip(),
    }


def _ls(c: BlobmanConfig, root_dir=None):
    fn = lambda p: glob.glob(p, recursive=True, include_hidden=False)
    paths = set(itertools.chain(*map(fn, c.include_patterns.values())))
    for p in set(paths):
        if Path(p).is_dir():
            paths |= set(glob.glob(str(Path(p) / '**'), recursive=True, include_hidden=False))
    for p in set(paths):
        if Path(p).is_dir():
            paths.discard(str(Path(p)))
            paths.add(os.path.join(p, ""))  # ensure trailing slash on dirs
    for p in sorted(paths):
        yield p


def _ensure_line(line, file):
    if line in map(str.strip, Path(file).read_text().split(os.linesep)):
        return
    with Path(file).open('a') as f:
        f.write(str(line) + os.linesep)


# get a list of filest currently managed by blobman
def _list_blob_files():
    ...


# get a list of files tracked by git
def _list_git_files():
    return run('git ls-tree -r HEAD --name-only').stdout.split(os.linesep)