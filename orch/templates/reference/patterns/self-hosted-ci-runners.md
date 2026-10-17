# Self-Hosted CI Runners (this account's pattern)

This account runs GitHub Actions self-hosted runners **on the orch VM
itself** for projects that build/push a Docker image or otherwise have a
CI job heavy enough to benefit from a local, always-warm machine instead of
a freshly-provisioned GitHub-hosted one. It is faster and avoids GitHub's
per-minute billing for hosted runners.

Two projects already use it: `odmarkj/asha` (`asha-vm-1`, `asha-vm-2`) and
`odmarkj/racepace` (`racepace-vm-1`). The reference implementations are
`asha-core-eval/.github/workflows/test-asha-core.yaml` (original) and
`racepace/.github/workflows/web-image.yaml` (second instance, copied from
the first). **Default to this pattern** for a new project's CI instead of
reaching for `runs-on: ubuntu-latest` out of not knowing it exists.

## Why not just `runs-on: self-hosted`

A self-hosted runner is not always online — the VM can be stopped, the
runner's systemd unit can be down, etc. A workflow that hardcodes
`runs-on: [self-hosted, <repo>-vm]` with no fallback loses CI entirely the
moment the runner is offline. The fix is a **probe-and-route** job that
checks runner availability via the GitHub API first and routes to
`ubuntu-latest` if the self-hosted runner isn't there — including on *any
probe failure* (API down, token rotated, `jq` surprise). A broken probe
must degrade to slower/pricier CI, never to no CI at all.

## The `pick-runner` snippet

Copy this job into any workflow that should prefer the local runner. Replace
`<repo>-vm` with `<your-repo-name>-vm` (the label convention below) and set
`<label-env>` to whichever secret name you use for `RUNNER_PROBE_TOKEN`.

```yaml
jobs:
  # Routes the build to the self-hosted VM runner when it's online, and to
  # GitHub's hosted runners when it isn't — same fail-safe pattern as
  # odmarkj/asha's CI (asha-core-eval's test-asha-core.yaml). A probe error
  # (API down, token rotated) counts as offline and takes the hosted path:
  # broken probe must degrade to slower/pricier CI, never to no CI at all.
  # Listing runners needs admin-level API access the workflow's own
  # GITHUB_TOKEN can't be granted, hence the PAT.
  pick-runner:
    runs-on: ubuntu-latest
    outputs:
      vm: ${{ steps.probe.outputs.vm }}
    steps:
      - id: probe
        env:
          GH_TOKEN: ${{ secrets.RUNNER_PROBE_TOKEN }}
        run: |
          online=$(gh api "repos/${{ github.repository }}/actions/runners" \
            --jq '[.runners[] | select(.status=="online")
                   | select([.labels[].name] | index("<repo>-vm"))] | length' \
            2>/dev/null || echo 0)
          if [ "${online:-0}" -ge 1 ]; then
            echo "vm=true" >> "$GITHUB_OUTPUT"
          else
            echo "vm=false" >> "$GITHUB_OUTPUT"
          fi
          echo "<repo>-vm runners online: ${online:-0}"

  build:
    needs: pick-runner
    runs-on: ${{ needs.pick-runner.outputs.vm == 'true' && fromJSON('["self-hosted", "<repo>-vm"]') || 'ubuntu-latest' }}
    # ...the rest of the real job, unchanged...
```

Key points:

- The probe job itself always runs on `ubuntu-latest` — it's a cheap API
  call, not something to route.
- `needs.pick-runner.outputs.vm == 'true' && fromJSON('["self-hosted", "<repo>-vm"]') || 'ubuntu-latest'` —
  this ternary-via-`&&`/`||` is the whole trick. `runs-on:` takes either a
  string or a list; `fromJSON` builds the list only when the probe says the
  runner is online.
- `secrets.RUNNER_PROBE_TOKEN` must be a **classic PAT with `repo` scope**,
  not the workflow's own `secrets.GITHUB_TOKEN`. The built-in token cannot
  be granted the admin-level access needed to list a repo's runners. A
  classic PAT scoped to `repo` on this personal account works across every
  repo the account owns, so the same PAT already used for `asha` and
  `racepace` can be reused for a new repo's `RUNNER_PROBE_TOKEN` secret —
  it does not need to be a fresh token per project.
- If multiple self-hosted runners might ever back one project, the probe
  counts runners carrying the label, not a specific name — `config.sh` sets
  a unique `--name` per runner (e.g. `racepace-vm-1`) but a shared `--labels
  <repo>-vm` so the probe only needs to know the one label.

## Label / naming convention

- **Runner name** (`config.sh --name`): `<repo>-vm-N` — unique per runner,
  numbered in case a project ever needs more than one (`asha` has two:
  `asha-vm-1`, `asha-vm-2`).
- **Runner label** (`config.sh --labels`, and what the probe checks):
  `<repo>-vm` — shared across every runner for that repo, no number.
- **Directory on the VM**: `/home/joshuaodmark.guest/gh-runners/<repo>-runner-N`.
- **systemd unit**: `actions.runner.<owner>-<repo>.<repo>-vm-N.service`,
  installed system-wide (not a `--user` unit) via the runner's own
  `svc.sh install <user>` — this is why setup needs `sudo`.

Important constraint: GitHub Actions self-hosted runners on a **personal
account repo** cannot be shared across repos. Each project needs its own
runner registration even though every runner is just another process on
this same VM — there is no way around registering once per repo.

## Automating the setup

`orch runner setup <owner>/<repo>` does the manual steps end to end — see
`orch runner setup --help`. It:

1. Requests a registration token from GitHub
   (`POST /repos/<owner>/<repo>/actions/runners/registration-token`).
2. Extracts a fresh copy of the runner into
   `/home/joshuaodmark.guest/gh-runners/<repo>-runner-N` from the cached
   `runner.tar.gz` already on this VM (no re-download needed).
3. Runs `config.sh --unattended` with the `<repo>-vm-N` name and `<repo>-vm`
   label.
4. Installs and starts the systemd service via `svc.sh install <user>` +
   `svc.sh start` (prompts for `sudo`).
5. Sets the `RUNNER_PROBE_TOKEN` repo secret via `gh secret set` (prompts
   for the PAT value unless passed with `--probe-token` or found in
   `RUNNER_PROBE_TOKEN_PAT`; the existing `asha`/`racepace` PAT can be
   reused — see above).

After that, copy the `pick-runner` snippet above into the project's
workflow, swap in the repo's `-vm` label, and CI will prefer the local
runner automatically.
