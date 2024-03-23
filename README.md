# Blobman
A simple large binary file manager alongside your VCS based on Restic backup tool.

## Why?
Pros over Git LFS:
- stored separately from main repository
- can switch remotes, prune, purge and repack
- clean up old blobs deep in history without messing with git history rewrite
- encrypted, so handles sensitive files such as:
    - .env files
    - salt's pillars
    - pyinfra's group_data

It is currently slow because it doesn't cache snapshots locally. During each checkout,
blobs are fetched from remote. Restic is smart about partial restores so not too slow.
Works for my use case though.

## Installation
To install blobman, initialize python virtual environment with
either poetry or virtualenv package. After this, install blobman
package from Gitea releases.
```shell
poetry source add --priority=explicit gitea https://gitea.bernardcrnkovic.from.hr/api/packages/bernard/pypi
poetry add --source gitea blobman
```
This utility assumes `restic`, `awk` & `shasum` are present on host system.

## Usage
Using blobman is simple:
```shell
blobman --help               # usage instructions
blobman init                 # type in restic repository URL and password when prompted...
blobman add 'my-pattern/**'  # add pattern to track
blobman snapshot --dry       # preview snapshot
blobman snapshot             # take snapshot
blobman status               # check status of your blobs
git checkout ...
blobman checkout             # when switching worktree, switch blobman snapshot to match pulled changes
```

## Developing new version
To modify blobman and release new version, add following remote to your
`pyproject.toml`:
```toml
[[tool.poetry.source]]
name = "gitea"
url = "https://gitea.bernardcrnkovic.from.hr/api/packages/bernard/pypi"
default = true
```

When publishing a new version:
```shell
poetry publish --build --repository gitea
```
