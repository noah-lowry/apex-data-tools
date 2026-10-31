# apex-data-tools

Data tools for Apex buggy practice: log rolls from a phone, pull recordings off Garmin
watches, and match the two up.

## Install

Needs [uv](https://docs.astral.sh/uv/). It fetches Python 3.14 if you don't have it.

```sh
uv tool install git+https://github.com/noah-lowry/apex-data-tools   # or, from a checkout: uv tool install .
apex --help
```

This puts `apex` on your PATH. If uv warns that its tool directory isn't on PATH, run
`uv tool update-shell` once. Update later with
`uv tool upgrade apex-data-tools`, or reinstall from a checkout with
`uv tool install --reinstall .`.

## Running the server

On whichever machine hosts it (a laptop at the field, or a server elsewhere):

```sh
apex serve --password <pw>
```

It prints the address phones should open (same Wi-Fi network) and a QR code. Scanning
the code opens the page and signs the phone in, because the code contains the password.
Use `--no-qr-login` for a code that only opens the page, so everyone types the password.

## Pointing a machine at the server

Each machine that runs `collect-rolls` needs to know the server and its password, once:

```sh
apex config set server http://192.168.1.20:8000   # `apex serve` prints this line for you
apex config set password                          # prompts; not echoed or kept in history
apex config                                       # show settings and test the connection
```

Settings are kept in `~/.config/apex/config.toml`, readable only by you. Use
`$APEX_CONFIG` to put it elsewhere. `--server`/`--password` flags, or the `$APEX_SERVER`/
`$APEX_PASSWORD` env vars, override it for one run. A machine that is itself running
`apex serve` needs no setup: with nothing configured, `collect-rolls` uses the local server.

## At the field

**Logging rolls (phone).** In the *Collection* tab, pick the buggy (required), the
driver and add notes, then *Create roll*. Pick from names used before, or choose
*+ New…* to add one. Choosing a buggy pre-fills whoever last drove it. The server
timestamps each entry, so rolls are numbered per buggy per day:
"Solaris Roll 3 (10/31/26)". Got something wrong? *Edit* on any roll changes its
buggy, driver or notes, or deletes it. Delete takes a second tap to confirm, and any
data assigned to the deleted roll goes back to *Unassigned data*.

**Collecting watch data (any configured machine).** When a buggy comes back, stop the watch, plug it in,
and run:

```sh
apex collect-rolls -b Solaris /run/media/$USER/GARMIN
```

This searches the directory recursively. It only sends activity recordings from today
(`--all` sends every day's). `-b` labels the files with the buggy they rode on, to help
match them up later. Pass `--no-buggy` if you don't know which buggy. Running it again is
safe: files are stored once, keyed by their content. An existing buggy label is never
overwritten; a missing one is filled in.

Times shown for a file are the roll's, not the watch's. The roll starts when the buggy
crosses the course's first gate, and ends at the last gate it is seen crossing. If GPS
drops out before the finish, the roll is marked *partial*. A recording that never
crosses the start gate shows *no roll detected*, along with how long the watch ran.

**Assigning data.** New files appear under *Unassigned data* on every phone within a few
seconds. Pick the roll each belongs to and press *Assign*. When the file's buggy label
matches a roll, that roll is pre-selected. Each roll takes one recording, so the dropdown
only offers rolls with no data yet. Only unassigned files can be assigned. To fix a
mistake, *Unassign* the file from its roll first. Both lists show today only; switch to
*All days* for older entries.

Once a roll has data, the data decides when it happened. The roll's time becomes the
moment the watch saw the buggy cross the start gate, not when the entry was created, and
its number and day follow from that. So entries logged late or out of order still end up
numbered in the order the rolls actually ran. The entry time stays visible as "logged …".
Buggy, driver and notes always come from the entry, never from the file's buggy label.

## doomscROLL

The analysis tab. It's a feed of every roll that has data, newest first, filterable by
buggy, driver and day.

**Cards.** Each card shows the roll's time, a speed trace coloured by segment, and its
split for each segment with the gap to that buggy's best roll. Badges mark a buggy's
**PB**, the **fastest of the day**, a **partial** roll (GPS lost before the finish), or a
roll that **stopped** on the course.

**Leaderboard.** It sits above the feed and follows the filters. It lists the fastest
rolls, the record for each segment, and each buggy's best and median time.

**Roll page.** Tap a card to open it:

- **Scoreboard and ranks:** where the roll places for its buggy, overall and on that day.
- **Splits table:** each segment's time, entry/exit/min/max speed, speed gained or
  lost, and heading change.
- **Speed trace:** the roll's speed along the course, with a comparison roll overlaid.
- **Time-gap trace:** where time was won and lost against the comparison.
- **Map:** the track coloured by speed, following the chart cursor.
- **Segment deep dive:** for any one segment, its own speed trace and map, every time
  the buggy has posted there, and whether carrying more entry speed paid off.

Rolls are compared with the buggy's best by default; pick any other roll instead. Both
traces are aligned at every gate, so the gap is exact there.

How far to trust the numbers:

- **Gate times** are interpolated at the exact crossing, so splits are precise.
- **Speeds** are the watch's Doppler readings.
- **Sampling** is one sample per second, so a short segment like the Chute has only
  about 9 samples. Its speed curve between samples is not known, and the page says so.
- **Bad GPS:** segments marked "bad GPS" (tree cover) are flagged throughout.
- **Map tiles** come from OpenStreetMap. Offline, the map still draws the track and
  gates on a plain background.

Analyses are computed from the stored raw files at ingest. When the analysis code
changes (`ANALYSIS_VERSION` in `src/apex/analysis.py`), every stored file is re-analysed
the next time the server starts.

## Data

Everything lives in `~/.local/share/apex` (override with `--data-dir` or
`$APEX_DATA_DIR`):

- `apex.db`: SQLite. Rolls, the buggy/driver roster, and a summary of each watch file.
- `raw/<ab>/<sha256>.fit`: the original FIT bytes, so every run can be re-parsed later.

## Development

```sh
uv sync
uv run pytest        # includes a test that builds the wheel and checks its contents
uv run apex --help   # run from the checkout without installing
```
