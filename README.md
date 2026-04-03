# PDT — Pending Delete Tracker

A terminal CLI for tracking domain names in pending-delete, monitoring their RDAP status, and getting desktop notifications before they drop.

---

## Features

- Track domains with drop time, appraisal value, and notes
- Auto-fetch RDAP status on add, or refresh on demand
- Responsive table output that adapts to any terminal width
- Machine-readable CSV output for scripting
- Live RDAP polling with real-time status change detection
- Background daemon that notifies you 5 minutes before a domain becomes available

---

## Requirements

- Python 3.9+
- [pipx](https://pipx.pypa.io)

---

## Installation

Clone the repository and install with `pipx`:

```sh
git clone https://github.com/danilopatrial/pdt.git
cd pdt
pipx install .
```

To pick up code changes after editing `pdt.py`:

```sh
pipx reinstall pdt
```

To uninstall:

```sh
pipx uninstall pdt
```

---

## Quick Start

```sh
# Add a domain expiring in 1 day, 3 hours, 57 minutes
pdt add example.com -t 1d3h57m -a 500 -n "good domain"

# List everything
pdt list

# Show the 5 soonest to drop
pdt next

# Start background notifications
pdt watch -d
```

---

## Commands

### `add`

Add a domain to track. RDAP status is fetched automatically unless you pass `-s`.

```sh
pdt add <domain> -t <duration> [-a <usd>] [-n <note>] [-s <status>]
```

| Flag | Description |
|---|---|
| `-t`, `--time` | Time until drop — required. Format: `1d3h57m`, `45m`, `2h30m` |
| `-a`, `--appraisal` | Estimated value in USD |
| `-n`, `--note` | Freeform note |
| `-s`, `--status` | Override RDAP status (skips the auto-fetch) |

```sh
pdt add dropcandidate.com -t 2h30m -a 1200 -n "short, memorable"
```

---

### `remove`

Remove a domain from tracking.

```sh
pdt remove example.com
```

---

### `update`

Update any field for a tracked domain. Updating `-t` resets the drop notification.

```sh
pdt update <domain> [-t <duration>] [-a <usd>] [-n <note>] [-s <status>]
```

```sh
pdt update example.com -t 45m          # refine the drop time
pdt update example.com -a 800 -n "revised"
```

---

### `list`

Display all tracked domains in a table sorted by drop time.

```sh
pdt list [--sort time|appraisal|domain|status] [-m]
```

| Flag | Description |
|---|---|
| `--sort` | Sort by `time` (default), `appraisal`, `domain`, or `status` |
| `-m`, `--machine` | CSV output for scripting |

The table adapts to your terminal width — columns are progressively hidden on narrow terminals and the Note column fills any remaining space on wide ones. Times are UTC.

```sh
pdt list --sort appraisal
pdt list -m | cut -d, -f1,5   # domain + status only
```

---

### `next`

Show the N domains closest to dropping.

```sh
pdt next [-n <count>] [-m]
```

```sh
pdt next -n 10
pdt next -m   # CSV output
```

---

### `rdap`

Fetch live RDAP status for one or more domains and update the tracked records.

```sh
pdt rdap <domain> [<domain> ...]
pdt rdap --all
```

| Flag | Description |
|---|---|
| `-a`, `--all` | Refresh every tracked domain |

```sh
pdt rdap example.com
pdt rdap example.com other.net
pdt rdap --all
```

---

### `poll`

Live-poll RDAP status on a repeating interval. The table refreshes in-place with a progress bar countdown. A desktop notification fires if a domain flips to `available`.

```sh
pdt poll [<domain> ...] [--next <n>] [-i <secs>]
```

| Flag | Description |
|---|---|
| `-n`, `--next N` | Poll the next N tracked domains (by drop time) instead of naming them |
| `-i`, `--interval` | Seconds between polls (default: `10`) |

```sh
pdt poll example.com other.net          # specific domains
pdt poll --next 5                        # 5 soonest tracked domains
pdt poll example.com -i 30              # poll every 30 seconds
```

Status changes are highlighted with `●` and the previous value is shown in a **Was** column when it appears. Press `Ctrl+C` to stop.

---

### `watch`

Background daemon that checks all tracked domains every 60 seconds and sends a desktop notification 5 minutes before drop time.

```sh
pdt watch           # foreground (Ctrl+C to stop)
pdt watch -d        # background daemon
pdt status          # check if daemon is running
pdt stop            # stop the daemon
```

Logs are written to `~/.pdt/daemon.log`.

Notification delivery is attempted via **plyer** → **notify-send** (Linux) → **osascript** (macOS), in that order.

---

## Data

All domain records are stored as JSON at `~/.pdt/domains.json`. You can back it up, sync it, or inspect it directly.

```sh
cat ~/.pdt/domains.json
```

---

## Duration Format

Drop times use a compact format: `1d3h57m`, `45m`, `2h`, `30s`. Any combination of `d`, `h`, `m`, `s` is valid. Used by `add` and `update -t`.

---

## CSV / Machine Output

Any command with `-m` / `--machine` outputs plain CSV for use in scripts:

```sh
pdt list -m
# domain,remaining_seconds,drop_time_utc,appraisal,status,note

pdt next -m
# domain,remaining_seconds,drop_time_utc,appraisal,status

pdt list -m | awk -F, '$2 < 3600'   # domains dropping in under 1 hour
```
