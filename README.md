# musicdl

Downloads audio from YouTube Music and tags it using MusicBrainz/TheAudioDB and gets lyrics from Genius/Musixmatch. Identification now runs through a chain of sources instead of a single MusicBrainz search, so far fewer songs end up
untagged.

## Install

```powershell
cd path\to\musicdl
py -m pip install -r requirements.txt
```

`yt-dlp` and FFmpeg must be on PATH:

```powershell
yt-dlp --version
ffmpeg -version
```

## Configuration

You will need to source your own cookies file for YouTube by downloading an extension for your browser.

Settings that are specific to your machine (folders, API keys, the artist
separator) live in `musicdl.ini`, not in the script, so the script can be
shared as is.

1. Copy `musicdl.example.ini` to `musicdl.ini`, in the same folder as
   `musicdl.py`.
2. Open `musicdl.ini` in any text editor and fill it in. Every setting is
   explained inside the file.

Where a setting can come from, strongest first: command-line flag,
environment variable, `musicdl.ini`, built-in default. To load a different
file use `--config PATH` or the `MUSICDL_CONFIG` environment variable.

Without a `musicdl.ini` the script still runs: it saves to `Music/musicdl` in
your home folder and says so at startup.

## Basic use

```powershell
py musicdl.py "https://music.youtube.com/playlist?list=YOUR_PLAYLIST"
py musicdl.py "https://music.youtube.com/watch?v=VIDEO_ID"
```

The library folder is set in `musicdl.ini`.

## How a song gets identified

For each file, in order, stopping at the first success:

1. **Manual override** you pinned in `.musicdl_overrides.json`
2. **Match cache** from previous runs
3. **MusicBrainz text search**, now as a cascade of up to 7 queries
4. **Album context** — match the title against a known album's tracklist
5. **AcoustID** audio fingerprinting (optional, needs a key + fpcalc)
6. **TheAudioDB**
7. **Second album-context pass** after every file in the run has been seen

### Why songs were being missed before

The old version ran exactly one strict quoted query. Any stray word in the
YouTube title — a guest credit, "(Sped Up)", "(Official Video)", a curly
apostrophe, an accented letter — returned zero results and the song was
given up on, even though it was sitting in MusicBrainz.

The cascade now retries with: version qualifiers stripped, unquoted tokens,
accent-folded text, title-only (dropping the noisy artist field), and
finally Solr's forgiving `dismax` parser.

### Album context — the fix for "one song on the album didn't match"

This is the specific case where every other track on an album matched but
one didn't, so that one got no track number.

Once any track on a release is identified, the script remembers that
release's full tracklist. Any file it can't identify normally is then
compared against those ~12 known track titles instead of against all of
MusicBrainz. With such a small candidate pool, a much looser similarity
threshold is safe, so guest credits and punctuation differences stop
mattering.

It runs twice: inline during pass 1, and again in pass 2 after every file
has been seen — because in pass 1 the odd track might be processed before
its siblings have established the album.

If a file has an album tag but no sibling matched, the script also searches
MusicBrainz for that album directly and tries its tracklist.

Disable with `--no-album-context`.

## AcoustID fingerprinting (recommended)

This identifies a file by what it actually sounds like, so it works even
when the title is useless. It's the most reliable fallback available.

Two setup steps:

1. Install Chromaprint so `fpcalc` is on PATH. Verify with `fpcalc -version`.
2. Get a free API key at https://acoustid.org/new-application

```powershell
py musicdl.py "URL" --acoustid-key YOUR_KEY
```

Without a key the step is silently skipped. If the key is given but `fpcalc`
isn't found, the script says so and carries on.

## Manual overrides — the guaranteed fix

For anything that still won't match, pin it by hand. Create
`.musicdl_overrides.json` inside your library folder:

```json
{
  "Some Song.m4a": { "recording": "https://musicbrainz.org/recording/<id>" },
  "Another Song.m4a": { "release": "https://musicbrainz.org/release/<id>" }
}
```

- `recording` pins the exact track.
- `release` pins the **album** and finds the track inside it by title.
  This is usually easier — you only have to look up the album once, and it
  guarantees the right track number.

Keys can be the filename, the filename without extension, `Artist - Title`,
or just the title. Full URLs or bare MBIDs both work.

After a run, any file that couldn't be identified is listed in
`.musicdl_unmatched.txt` with its artist/title/album, ready to paste into
the overrides file.

## Picking from a list

```powershell
py musicdl.py "URL" --pick
```

On a weak match, shows up to 8 numbered candidates with their releases and
scores so you can rescue a near-miss instead of rejecting it.

## Track numbers

- In a multi-track release (album/EP): its real position, e.g. `Track 4 of 12`
- Anything else, including standalone singles: `Track 1 of 1`

YouTube playlist position is never used as a track number.

## Artwork

Every file ends up with a 1:1 cover, including songs that couldn't be matched:
their 16:9 YouTube thumbnail is center-cropped to a square (which also strips
the black bars from YouTube Music's pillarboxed thumbnails).

Always a square 1200x1200 JPEG. Art is taken from the Cover Art Archive for
the matched release, then the release group, then TheAudioDB. If none has
art, the thumbnail yt-dlp embedded is center-cropped to square rather than
left at YouTube's non-square size.

## Other options

```text
--yes              accept lower-confidence matches without prompting
--force            ignore the cache and search again
--no-download      tag existing M4A files only
--rename           rename to "Artist - Song.m4a" (never overwrites)
--folder PATH      use a different library folder
--no-audiodb       disable the TheAudioDB fallback
--audiodb-key KEY  use your own TheAudioDB key instead of the shared test key
--config PATH      settings file (default: musicdl.ini next to the script)
--cookies PATH     cookies.txt for yt-dlp (default: from musicdl.ini)
--acoustid-key KEY enable audio fingerprint matching
--no-album-context disable album/tracklist matching
--pick             choose from a numbered candidate list on weak matches
--musixmatch-key K / --genius-token T   lyrics sources
--no-lyrics        skip lyrics
--relyrics         replace lyrics already in the files
--partial-lyrics   accept Musixmatch's 30% free-plan preview
--clear-cache      delete .musicbrainz_cache.json before running
```

## Files written to the library folder

```text
.musicbrainz_cache.json    match + API cache (all sources)
.musicdl_overrides.json    your manual pins (you create this)
.musicdl_unmatched.txt     report of what failed, rewritten each run
.musicdl_run_files.txt     temporary; deleted after each download
```

## Recommended first run

```powershell
py musicdl.py "unused" --no-download --force --acoustid-key YOUR_KEY --yes
```

`--force` is important: it ignores cached failures from the old version so
everything gets re-examined with the new logic.

## Re-tagging: every file yt-dlp touches, every run

Earlier versions only tagged files that were brand new, by comparing the
folder before and after the download. A song that was already in the folder
never showed up as "new", so it was skipped. (It had nothing to do with the
`.musicbrainz_cache.json` file, which only speeds up lookups and never
decides whether a file gets tagged.)

Now yt-dlp is asked to report every file it downloaded or re-processed, and
all of those are tagged again, including songs already in the folder. The
output says how many: `Tagging 12 file(s) from this run (3 already in the
folder).`

This needs a reasonably recent yt-dlp. If yours is too old to support that,
the script says so, falls back to the old behaviour, and tells you to run
`py -m pip install -U yt-dlp`.

You never need to edit the cache file. To wipe it, add `--clear-cache`.
It lives in your library folder as `.musicbrainz_cache.json`.

## Separate contributing artists

"charli xcx & A.G. Cook" becomes "charli xcx / A.G. Cook" in the artist field:
the artists are split apart and rejoined with a single slash. The Album artist
field gets the primary artist only. A `----:com.apple.iTunes:ARTISTS` tag
holding each artist as its own value is also written for players that read it.

To change the separator, set `artist_separator` in `musicdl.ini` (for example
`"/"` or `"; "`).

- For songs matched on MusicBrainz this uses MusicBrainz's own credit, which
  is already split into individual artists, so it's exact.
- Otherwise the name is split on `feat.`, `ft.`, `;`, `vs.` (always) and on
  `&`, `,`, `and`, `x`, `with`, `/` (only after checking MusicBrainz doesn't
  know the whole string as one artist). That check is what keeps
  "Simon & Garfunkel", "Earth, Wind & Fire", "Tyler, The Creator" and
  "AC/DC" in one piece.
- Files that couldn't be identified get the same split on their existing
  artist tag. Their title, album and track number are left alone.

## Lyrics (Musixmatch, then Genius)

Lyrics go in the standard lyrics tag, so Explorer, MusicBee, foobar2000,
Poweramp and most phone players show them.

```powershell
py musicdl.py "URL" --musixmatch-key KEY --genius-token TOKEN
```

To avoid retyping them, put the keys under `[keys]` in `musicdl.ini`, or set
the `MUSIXMATCH_API_KEY` and `GENIUS_ACCESS_TOKEN` environment variables.
With neither set, lyrics are skipped.

- **Musixmatch** is the official API. Its free plan only returns about 30%
  of each song, and flags it as such. A preview like that is treated as "not
  found" and Genius gets a turn. Accept previews anyway with
  `--partial-lyrics`.
- **Genius**: its API only searches, it doesn't hand out lyrics. The text is
  read from the song's public page. That can break if Genius changes its
  layout or blocks the request, and it isn't something Genius's terms
  welcome. Treat it as best-effort.
- Files that already have lyrics are left alone. `--relyrics` replaces them.
- "Not found" is remembered so reruns don't ask again. It is retried
  automatically when you add a new key or token, or with `--force`.
- `--no-lyrics` turns it off.
- Lyrics are fetched for unidentified files too, using their YouTube
  artist and title.

## Cookies

If you set `cookies` under `[paths]` in `musicdl.ini`, yt-dlp is passed that
file, so your own private and liked playlists resolve. Export it from your browser in
Netscape format with a cookies.txt extension.

If the file isn't there the script says so and downloads without it. Point
somewhere else with `--cookies "path\to\cookies.txt"`.

Treat the file like a password — it is your live logged-in session.

## Important

The downloader uses yt-dlp normally, with your own cookies if you supply
them. It contains no PO-token generation, age-restriction bypassing, or DRM
circumvention. If a video can't be downloaded normally it's skipped and the
playlist continues.

Request rates are throttled to each service's stated limit: MusicBrainz
~1/sec, TheAudioDB ~2/sec, AcoustID ~3/sec.
