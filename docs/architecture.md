# Architecture

## End-to-end data flow

```
[ Sonarr/Radarr ] -- search/grab --> [ qBittorrent on seedbox ]
                                              |
                                              | (downloads, then keeps seeding;
                                              |  Prowlarr manages ratio/seedtime)
                                              v
                          /home14/user/downloads/qbittorrent/remote-{tv,movies}/<release>
                                              |
                                              |  (rclone SFTP, NAS pulls)
                                              v
[ seedbox-fetcher container on NAS ] -- rclone copy --> /mnt/user/data/torrents/remote-{tv,movies}/<release>
                                              |
                                              v
[ Sonarr/Radarr import: copy + delete original ] --> /mnt/user/data/media/{tv,movies}/...
                                              |
                                              v
                                       [ Plex serves ]

[ leftover release folders 24h+ old ] -- cleanUpDownloadStaging.sh --> deleted
                                       (fail-closed on Sonarr/Radarr queue API)

[ Usenet path: SABnzbd local on NAS --> /mnt/user/data/usenet/{tv,movies} --> *arr import ]
[ sab-watchdog polls SAB and unsticks jobs that hang in post-processing ]
```

## seedbox-fetcher internals

Three threads, sharing state via locks:

```
+--------------+        queue.Queue        +--------------+
|   poller     |  ─── PullJob ───────▶     |   worker     |
| (poll cycle) |                            | (rclone copy)|
+--------------+                            +--------------+
       │                                            │
       │   updates AppState                         │   updates AppState
       ▼                                            ▼
              +-----------------------+
              |       AppState        |
              | + PersistentState (yml)
              | + current_pull        |
              | + metrics counters    |
              | + log ring (500)      |
              | + sse_seq (Condition) |
              +-----------------------+
                          ▲
                          │  reads
                          │
              +-----------------------+
              | FastAPI + uvicorn     |
              | (Web UI, /metrics,    |
              |  SSE, action POSTs)   |
              +-----------------------+
```

### State machine for a release

```
              first-time observed (passes bootstrap)
                      │
                      ▼
               ┌─────────────┐  size/count changed
               │ waiting     │ ◀──────────────────────┐
               │ stable      │                        │
               └──────┬──────┘                        │
                stable for N seconds                  │
                      │                               │
                      ▼                               │
               ┌─────────────┐                        │
               │ already on  │ ─yes→ marked pulled    │
               │ NAS?        │                        │
               └──────┬──────┘                        │
                      │ no                            │
                      ▼                               │
               ┌─────────────┐                        │
               │ arr-history │ ─skip→ arr_skipped     │
               │ check       │                        │
               └──────┬──────┘                        │
                      │ pull                          │
                      ▼                               │
               ┌─────────────┐                        │
               │ enqueued    │                        │
               └──────┬──────┘                        │
                      ▼                               │
               ┌─────────────┐                        │
               │ pulling     │                        │
               └──────┬──────┘                        │
              success │ │ cancel/fail                 │
                      │ └──────────────────────────► retry on next cycle
                      ▼
               ┌─────────────┐
               │ pulled      │
               └─────────────┘
```

### Filters in order

| Filter | Purpose | Where decided |
|--------|---------|---------------|
| `pulled: true` in state | Already handled | poller |
| Cutoff mtime | Pre-existing media before this tool was installed | poller |
| Stability gate | Wait for download to finish on the seedbox | poller |
| Local exists | Don't re-pull what's already on the NAS | poller |
| *arr history (opt) | Don't re-pull what *arr already imported | poller |

## sab-watchdog internals

One thread, one timer. Pulls SAB queue every poll cycle. For each in-flight
job, computes a "fingerprint" (`(mb_left, percentage, status, filename)`).
If the fingerprint hasn't changed in N minutes AND the status string matches
a stuck-state pattern, escalates:

1. At `alert_after_min` (default 30) → Apprise notify.
2. At `bounce_after_min` (default 45) → `pause_pp` + `resume_pp`.
3. At `restart_after_min` (default 75) → `restart_repair`.

Never deletes a job. *arr's Failed Download Handling cleans up if the job
ultimately aborts.

## Why pull instead of push?

The original design (Syncthing + a post-download hook) had two problems:

- The hook depends on qBit's `%L` (category) being correct at completion time.
  Cross-seed, manual adds, RSS races, and category-rename edge cases all
  silently break this.
- Syncthing has no concept of "wait until the file is done writing" beyond
  the simple file-watcher delay, and conflicts can land if the file changes
  on the source side mid-sync.

Pulling from the NAS solves both:

- The NAS picks up *any* completed release under the watched seedbox
  directories, regardless of qBit metadata correctness.
- A size+file-count stability gate guarantees the seedbox file is quiet
  before we touch it. No mid-write reads.

## Why pull instead of an *arr Remote Path Mapping?

You could let Sonarr/Radarr SSH into the seedbox themselves via Remote Path
Mapping. We don't, for two reasons:

- It couples *arr's import timing to the seedbox link being healthy at
  exactly that moment. With fetcharr, the NAS can pull and import on its
  own schedule, fully decoupled.
- This setup explicitly uses "copy + delete original" inside the *arr
  containers — fetcharr makes the staging folder cleanup observable
  (the GUI + the cleanup script's logs).
