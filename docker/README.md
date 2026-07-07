# openpilot dev container (Ubuntu 24.04 / noble)

A minimal Ubuntu 24.04 image with the prerequisites `tools/op.sh setup` needs
(build tools, git, git-lfs, file, curl, locales). Intended for running openpilot
dev tooling in a container while bind-mounting a local checkout.

## Build

```sh
docker build -t openpilot-noble:latest -f tools/docker/Dockerfile.noble tools/docker
```

## Run (bind-mount your checkout)

Mount the repo at `/app/openpilot` and keep the container alive:

```sh
docker run -d --name openpilot-noble \
  -v /path/to/openpilot:/app/openpilot -w /app \
  openpilot-noble:latest
```

On Windows (PowerShell), use the absolute host path:

```powershell
docker run -d --name openpilot-noble `
  -v C:\path\to\openpilot:/app/openpilot -w /app `
  openpilot-noble:latest
```

## Set up openpilot inside the container

```sh
docker exec -it openpilot-noble bash -lc "cd openpilot && tools/op.sh setup"
```

## Fix Windows-broken symlinks (required before building)

If the checkout was done by Git for Windows without symlink support
(`core.symlinks=false`), openpilot's repo symlinks (`msgq`, `opendbc`,
`rednose`, `teleoprtc`, `tinygrad`, ...) are checked out as plain text files
holding their target path, and `scons` fails with
`File ... found where directory expected`. Recreate them as real symlinks from
inside the container (Linux can create symlinks on the bind mount):

```sh
docker exec -it openpilot-noble bash -lc 'cd openpilot && git config core.symlinks true && \
  git ls-files -s | awk "\$1==\"120000\"{print \$4}" | while read -r f; do \
    [ -L "$f" ] || { t=$(cat "$f"); rm -f "$f"; ln -s "$t" "$f"; }; \
  done'
```

## Build

```sh
docker exec -it openpilot-noble bash -lc "cd openpilot && source .venv/bin/activate && scons -u -j\$(nproc)"
```

## Notes

- The checkout must have LF line endings (scripts fail to run with CRLF). On
  Windows: `git config core.autocrlf false` then re-checkout the tree.
- `.venv` created inside the container lives in the bind-mounted folder. Package
  installs across the Windows<->Docker boundary are slow; for heavy dev work,
  clone inside WSL2 instead of bind-mounting from Windows.
- The symlinks recreated above are real Linux symlinks. Git *on the Windows host*
  can't read them (`git status` shows them as deleted / "Function not
  implemented"); this is expected. Inside the container git sees them correctly.
  For this reason, run git operations from inside the container.
