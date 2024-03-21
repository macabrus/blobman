import datetime
import glob
import itertools
import json
import os
from pathlib import Path
import random
import textwrap
from attr import field
import cattrs
import click
from invoke import run
from attrs import define
from cattrs import register_structure_hook, register_unstructure_hook


def _git_root(start=os.getcwd()):
    p = Path(start)
    while not (p / '.git').is_dir():
        if p == p.root:
            return None
        p = p.parent
    return str(p)


GIT_ROOT = Path(_git_root())
BLOBMAN_PATH = GIT_ROOT / Path('.blobman/')
CONFIG_PATH = BLOBMAN_PATH / 'config.json'
LOCK_PATH = BLOBMAN_PATH / 'lock.json'
LIST_PATH = BLOBMAN_PATH / 'list.txt'
PASSWORD_PATH = BLOBMAN_PATH / 'password.txt'


@click.group
def cli():
    ...


@cli.command(help='initialize blobman config files & restic repository')
def init():
    if not _git_root():
        print('Error: not a git repository')
    first_run = True
    if BLOBMAN_PATH.exists():
        first_run = False
    BLOBMAN_PATH.mkdir(parents=True, exist_ok=True)
    c = BlobmanConfig()
    if not CONFIG_PATH.exists():
        _store_config(c)
    if not LOCK_PATH.exists():
        store_lock(c)
    if not PASSWORD_PATH.exists():
        PASSWORD_PATH.write_text('[REPLACE CONTENT WITH YOUR PASSWORD]')
    gi = Path('.gitignore')
    gi.touch(exist_ok=True)
    _ensure_line('.blobman/password.txt', '.gitignore')
    if first_run:
        print(
            textwrap.dedent(
                '''
                Configuration was initialized successfully.
                To continue, perform following steps:
                    1. Set "repository_url" field in .blobman/config.json to desired restic remote
                    2. Set password manually in .blobman/password.txt
                    3. Re-run 'blob init' to complete initializing restic repository
                '''
            )
        )
    else:
        c = _load_config()
        try:
            run('restic init', env=_get_env(c))
        except:
            pass
        run(f'git add {LOCK_PATH} {CONFIG_PATH}')


@cli.command(help='take a snapshot of local blobs')
def snapshot():
    c = _load_config()
    env = _get_env(c)
    LIST_PATH.write_text('\n'.join(_ls(c)))
    res = run(f'restic backup --files-from-verbatim {str(LIST_PATH)}', env=env)
    c.snapshot_id = res.stdout.split('\n')[-2].split(' ')[1]
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


register_unstructure_hook(datetime.datetime, datetime.datetime.isoformat)
register_structure_hook(datetime.datetime, datetime.datetime.fromisoformat)


@define(kw_only=True)
class LockInfo:
    timestamp: datetime.datetime | None = None
    tracked_files: list[dict[str, str]] = field(factory=list)


@define(kw_only=True)
class BlobmanConfig:
    repository_url: str = '[REPLACE WITH RESTIC URL]'
    repository_password: str = None
    snapshot_id: str | None = None
    include_patterns: dict[str, str] = field(factory=dict)
    lock: LockInfo = field(factory=LockInfo)


def _load_config() -> BlobmanConfig:
    if not CONFIG_PATH.exists():
        print('Error: blobman repository not initialized for this git repository')
        return
    return cattrs.structure(json.loads(CONFIG_PATH.read_text()), BlobmanConfig)


def _store_config(config: BlobmanConfig) -> None:
    j = cattrs.unstructure(config)
    del j['repository_password']
    del j['lock']
    CONFIG_PATH.write_text(json.dumps(j, indent=4))


def store_lock(config: BlobmanConfig) -> None:
    j = cattrs.unstructure(config.lock)
    LOCK_PATH.write_text(json.dumps(j, indent=4))


def _get_env(c):
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