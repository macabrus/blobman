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

    run('restic init', hide=True, env=_get_env(c))
    run(f'git add {VCS_LOCK_PATH} {CONFIG_PATH}', hide=True)

    _ensure_line('.blobman/password.txt', GITIGNORE_PATH)
    _ensure_line('.blobman/worktree-lock.json', GITIGNORE_PATH)
    _store_config(c)



@cli.command(help='take a snapshot of local blobs')
@click.option('--dry', 'dry', is_flag=True, default=False, help='show what will be snapshoted')
def snapshot(dry):
    c = _load_config()
    ensure_no_collisions(c)
    if dry:
        worktree = set(p['file'] for p in c.worktree_lock.tracked_files)
        remote = set(p['file'] for p in c.vcs_lock.tracked_files)
        common = worktree.intersection(remote)
        print('\nCommon files:\n')
        print('\n'.join(common))
        print('\nAdded files:\n')
        print('\n'.join(worktree - remote))
        print('\nRemoved files:\n')
        print('\n'.join(remote - worktree))
        return
    files = list(_ls(c))
    c.worktree_lock = c.vcs_lock = LockInfo(
        tracked_files=[{'file': f} for f in files]
    )
    LIST_PATH.write_text(os.linesep.join(files))
    res = run(f'restic backup --files-from-verbatim {str(LIST_PATH)}', env=_get_env(c))
    os.remove(LIST_PATH)
    c.vcs_lock.snapshot_id = res.stdout.split('\n')[-2].split(' ')[1]
    _store_config(c)


@cli.command(help='ensure latest blobs are present')
def checkout():
    c = _load_config()
    env = _get_env(c)
    run(f'restic restore {c.snapshot_id} --target "{os.getcwd()}"', env=env)


@cli.command(help='list exact files & snapshot history')
@click.option('--diff', is_flag=True, default=False, help='show diff of local & remote blobs')
def status(diff):
    c = _load_config()

    print('Patterns:\n')
    for id, pat in c.include_patterns.items():
        print(id, '-', pat)

    print('\nTracked blobs:\n')
    for p in _ls(c, root_dir=_git_root()):
        print(p)

    print('\nSnapshots:\n')
    snapshots = json.loads(run('restic snapshots --json', hide=True, env=_get_env(c)).stdout)
    print('\n'.join(s['id'][:8] + ' - ' + s['time'] for s in snapshots))


@cli.command(help='track glob pattern')
@click.argument('pattern')
def add(pattern):
    c = _load_config()
    if pattern in c.include_patterns.values():
        return
    while (id := hex(random.getrandbits(32))[2:]) in c.include_patterns:
        pass
    c.include_patterns[id] = pattern
    files = list(_ls(c))
    c.worktree_lock = LockInfo(
        tracked_files=[
            {
                'file': f,
                'hash': run(f'shasum {f} | awk "{{ print $1 }}"', hide=True).stdout.strip()
            }
            for f in files
        ]
    )
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
            'vcs_lock': json.loads(VCS_LOCK_PATH.read_text()),
            'worktree_lock': json.loads(WORKTREE_LOCK_PATH.read_text())
        },
        BlobmanConfig
    )


def _store_config(config: BlobmanConfig) -> None:
    o = cattrs.unstructure(config)
    PASSWORD_PATH.write_text(o.pop('repository_password'))
    VCS_LOCK_PATH.write_text(json.dumps(o.pop('vcs_lock'), indent=4))
    WORKTREE_LOCK_PATH.write_text(json.dumps(o.pop('worktree_lock'), indent=4))
    CONFIG_PATH.write_text(json.dumps(o, indent=4))


def _get_env(c: BlobmanConfig):
    return {
        'PWD': _git_root(),
        'RESTIC_REPOSITORY': c.repository_url,
        'RESTIC_PASSWORD': c.repository_password,
    }


def _ls(c: BlobmanConfig, root_dir=None):
    fn = lambda p: glob.glob(p, recursive=True, include_hidden=False)
    paths = set(itertools.chain(*map(fn, c.include_patterns.values())))
    for p in set(paths):
        if Path(p).is_dir():
            paths |= set(glob.glob(str(Path(p) / '**'), recursive=True, include_hidden=False))
    for p in set(paths):
        if not Path(p).is_file():
            paths.discard(p)  # discard non-regular-files
    for p in sorted(paths):
        yield p


# ensure no collisions between tracked files and git files
def ensure_no_collisions(c: BlobmanConfig):
    common = set(_ls(c)).intersection(set(_list_git_files()))
    if common:
        print('Error: collision between git tracked files and blobs\n')
        print('\nFollowing files are tracked both by .git and blobman:\n')
        print('\n'.join(common))
        print('\nIn order to fix a problem, you must correct following patterns:\n')
        print('... TODO')
        exit(1)


def _ensure_line(line, file):
    if line in map(str.strip, Path(file).read_text().split(os.linesep)):
        return
    with Path(file).open('a') as f:
        f.write(str(line) + os.linesep)


# get a list of files tracked by git
def _list_git_files():
    return run('git ls-tree -r HEAD --name-only', hide=True).stdout.split(os.linesep)