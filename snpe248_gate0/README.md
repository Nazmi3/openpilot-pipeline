# SNPE 1.41 -> QAIRT 2.48 on the comma two: staged Gate 0/1

Everything here was extracted from the public QAIRT 2.48 zip by HTTP range
requests (no 2.3 GB download): 5 Android libs + 65 headers.

## Rollback design

The port is ordered so that **the risky step is last and every step before it is
provably reversible**.

| gate | what it writes on the device | rollback |
|---|---|---|
| 0 — runtime available? | `/data/tmp/snpe248/` only | `rm -rf` (`rollback_gate0.sh`) |
| 1 — load + run a .dlc | same dir, reads models read-only | same |
| 2 — timing | nothing new | same |
| 3 — build modeld against 2.48 | `/data/openpilot` source + build outputs | see below |
| 4 — thneed | `models/supercombo.thneed` | `supercombo_backup.thneed` already on device |

Gates 0–2 never touch `/data/openpilot`. `rollback_gate0.sh` proves it by running
`git status --porcelain` afterwards — empty output means byte-identical.

### Before gate 3 (the only destructive one)

Build outputs are not in git, so snapshot them first:

```bash
ssh -p 8022 comma@172.20.10.2 \
  'cd /data/openpilot && tar czf /data/tmp/modeld_backup.tgz \
     selfdrive/modeld/_modeld selfdrive/modeld/modeld \
     selfdrive/modeld/SConscript third_party/snpe && \
   git rev-parse HEAD > /data/tmp/modeld_backup.head && echo ok'
```

Then gate 3 is reversible with:

```bash
ssh -p 8022 comma@172.20.10.2 \
  'cd /data/openpilot && git checkout -- . && \
   tar xzf /data/tmp/modeld_backup.tgz && echo restored'
```

Two rules that keep gate 3 cheap to undo:

1. Put the 2.48 libs in a **new** directory (`third_party/snpe/aarch64-248/`).
   Never overwrite `third_party/snpe/aarch64*`, so the 1.41 runtime stays intact
   and `dmonitoringmodeld` keeps working untouched.
2. Build with `scons --no-thneed` so `modeld` reads `models/supercombo.dlc`
   directly. The existing `supercombo.thneed` is left alone as a fallback.

## Running gate 0/1

```bash
bash deploy_gate0.sh
```

Override with `DEV=`, `PORT=`, `DEV_DIR=` if needed. It uploads, builds on device
with `clang++`, then runs the probe twice: once bare (gate 0) and once against
`/data/openpilot/models/supercombo.dlc` (gate 1, opened read-only).

## Reading the result

- `GPU available : no` -> stop, the whole 2.48 idea is dead; back to sourcing a 1.x SDK.
- `build() FAILED (graph prepare)` -> library loads but the graph won't prepare for
  Adreno 530. Also fatal, but a different cause worth reporting.
- `steady state : N ms` -> under ~50 ms means `--no-thneed` alone may be viable and
  gate 4 is optional. Well over means thneed is mandatory and the port gets harder.

Do this with the car parked. A half-ported `modeld` means no lateral control.
