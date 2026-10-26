#!/usr/bin/env python3
"""
musicdl.py
Download authorized YouTube/YouTube Music audio into a flat music library,
then identify/tag it using, in order of preference:

    1. A manual override you pinned yourself (.musicdl_overrides.json)
    2. The match cache
    3. MusicBrainz text search (multi-stage query cascade)
    4. Album context -- if the album is already known from sibling tracks or
       from the album tag, match the title against that album's tracklist
    5. AcoustID acoustic fingerprinting (optional; needs fpcalc + a free key)
    6. TheAudioDB
    7. A second album-context pass once every file in the run has been seen

Design goals:
- Flat library folder (set in musicdl.ini)
- AAC/M4A when the source exposes it
- YouTube metadata is only a starting point
- Track/disc numbers: a recording placed inside a multi-track release
  (album/EP) gets its real position there. Everything else is Track 1 of 1.
- Square 1200x1200 cover art on every file, every time -- including files
  that couldn't be identified, whose YouTube thumbnail is cropped to 1:1
- Combined artists ("A & B") are stored as separate contributing artists
- Lyrics from Musixmatch, with Genius as the fallback (stored in the lyrics tag)
- Every file yt-dlp touches is tagged again, including songs that were
  already sitting in the folder
- One bad/restricted/unavailable item never stops the whole playlist
- Optionally passes your own exported cookies.txt to yt-dlp so your private
  and liked playlists resolve. No PO-token generation, no DRM circumvention.

Requirements:
    py -m pip install -r requirements.txt

Examples:
    py musicdl.py "https://music.youtube.com/playlist?list=..."
    py musicdl.py "URL" --yes
    py musicdl.py "URL" --pick                 # choose from a candidate list
    py musicdl.py "URL" --acoustid-key KEY     # enable fingerprint matching
    py musicdl.py "unused" --no-download --force
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import requests
from mutagen.mp4 import MP4, MP4Cover

# ----------------------------- settings -------------------------------------
# Everything specific to your machine lives in musicdl.ini (copy
# musicdl.example.ini to musicdl.ini and edit it), so this file can be shared
# as is. Precedence: command-line flag > environment variable > musicdl.ini >
# built-in default.


def _config_path() -> Path:
    for i, arg in enumerate(sys.argv):
        if arg == "--config" and i + 1 < len(sys.argv):
            return Path(sys.argv[i + 1]).expanduser()
        if arg.startswith("--config="):
            return Path(arg.split("=", 1)[1]).expanduser()
    if os.environ.get("MUSICDL_CONFIG"):
        return Path(os.environ["MUSICDL_CONFIG"]).expanduser()
    return Path(__file__).resolve().with_name("musicdl.ini")


def _load_config():
    path = _config_path()
    parser = configparser.ConfigParser(interpolation=None)
    if not path.is_file():
        return parser, None
    try:
        parser.read(path, encoding="utf-8-sig")  # tolerate the BOM Notepad adds
    except configparser.Error as exc:
        print(f"Could not read {path}: {exc}\nUsing built-in defaults instead.\n")
        return configparser.ConfigParser(interpolation=None), None
    return parser, path


_CONFIG, CONFIG_PATH = _load_config()


def _setting(section: str, key: str, default: str = "", env: str = "") -> str:
    if env and os.environ.get(env, "").strip():
        return os.environ[env].strip()
    value = _CONFIG.get(section, key, fallback="").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]  # quotes let a value keep leading/trailing spaces
    return value or default


LIBRARY = Path(_setting("paths", "library", str(Path.home() / "Music" / "musicdl"))).expanduser()
_cookies = _setting("paths", "cookies")
# Netscape-format cookies.txt exported from your browser. Lets yt-dlp resolve
# your own private/liked playlists. Optional.
COOKIES_FILE: Optional[Path] = Path(_cookies).expanduser() if _cookies else None
CACHE_FILE = LIBRARY / ".musicbrainz_cache.json"
OVERRIDES_FILE = LIBRARY / ".musicdl_overrides.json"
UNMATCHED_FILE = LIBRARY / ".musicdl_unmatched.txt"
RUN_LIST_FILE = ".musicdl_run_files.txt"  # temporary: files yt-dlp handled this run

# How several contributing artists are joined in the artist field, e.g. "A / B".
ARTIST_SEPARATOR = _setting("general", "artist_separator", " / ")
# MusicBrainz asks every client to identify itself with a way to reach you.
CONTACT = _setting("general", "contact")
USER_AGENT = f"musicdl/1.2 (music tagger; contact: {CONTACT or 'unset'})"

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
MB_DELAY = 1.05  # MusicBrainz asks applications not to exceed 1 request/sec.

ADB_BASE = "https://www.theaudiodb.com/api/v1/json"
ADB_API_KEY = _setting("keys", "theaudiodb", "2", env="THEAUDIODB_API_KEY")  # "2" = shared free test key
ADB_DELAY = 0.6

ACOUSTID_BASE = "https://api.acoustid.org/v2/lookup"
ACOUSTID_DELAY = 0.4  # AcoustID allows roughly 3 requests/sec.
ACOUSTID_API_KEY = _setting("keys", "acoustid", env="ACOUSTID_API_KEY")

# Lyrics: Musixmatch is tried first, Genius second. Keys come from musicdl.ini,
# the MUSIXMATCH_API_KEY / GENIUS_ACCESS_TOKEN environment variables, or flags.
MUSIXMATCH_BASE = "https://api.musixmatch.com/ws/1.1"
MUSIXMATCH_API_KEY = _setting("keys", "musixmatch", env="MUSIXMATCH_API_KEY")
MUSIXMATCH_DELAY = 0.35
GENIUS_API_BASE = "https://api.genius.com"
GENIUS_ACCESS_TOKEN = _setting("keys", "genius", env="GENIUS_ACCESS_TOKEN")
GENIUS_DELAY = 0.6

REQUEST_TIMEOUT = 30
WEAK_MATCH_THRESHOLD = 75  # out of 135; below this, ask for confirmation.

# Album-context matching compares a title against ~12 known tracklist entries
# instead of against all of MusicBrainz, so it can safely use a looser
# threshold than a global search ever could.
TRACKLIST_SIMILARITY = 0.62          # release confirmed by a sibling track
TRACKLIST_SIMILARITY_STRICT = 0.78   # release merely guessed from the album tag


# ----------------------------- text helpers ---------------------------------


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def unicode_fold(value: str) -> str:
    """
    Normalize the unicode oddities YouTube titles are full of and that
    silently break exact-text search: curly quotes, en/em dashes, full-width
    characters, non-breaking spaces.
    """
    value = unicodedata.normalize("NFKC", clean_text(value))
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201b": "'",
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u2015": "-",
        "\u00a0": " ", "\u200b": "",
    }
    for bad, good in replacements.items():
        value = value.replace(bad, good)
    return value


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def norm(value: str) -> str:
    """Aggressively flatten text for comparison (never for searching)."""
    value = strip_accents(unicode_fold(value)).casefold()
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", value)
    value = re.sub(r"\b(feat\.?|ft\.?|featuring|with)\b.*$", "", value)
    value = value.replace("&", " and ")
    value = re.sub(r"\bpt\.?\b", "part", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_SEARCH_JUNK = re.compile(
    r"\b("
    r"official\s+(music\s+)?video|official\s+audio|official\s+lyric\s+video|"
    r"official\s+visualizer|lyric\s+video|music\s+video|visualizer|"
    r"audio\s+only|full\s+album|lyrics|hd|hq|4k|mv"
    r")\b",
    re.IGNORECASE,
)

# Version qualifiers. These are real information and stay in the written
# tags, but they are stripped when building looser fallback queries, since
# MusicBrainz very often indexes the plain title instead.
_VERSION_QUALIFIER = re.compile(
    r"\s*[\(\[]?\b("
    r"sped\s*up|slowed(\s*\+?\s*reverb)?|nightcore|remaster(ed)?(\s*\d{4})?|"
    r"radio\s*edit|extended(\s*(mix|version))?|single\s*version|album\s*version|"
    r"live(\s*(version|at|from).*)?|acoustic(\s*version)?|demo|instrumental|"
    r"bonus\s*track|deluxe|clean|explicit|mono|stereo|re-?recorded(\s*version)?"
    r")\b[^\)\]]*[\)\]]?\s*",
    re.IGNORECASE,
)

_TOPIC_CHANNEL = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)

# Lucene/Solr reserved characters, matching the MusicBrainz server's own escaper.
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def escape_lucene(value: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", value)


def escape_phrase(value: str) -> str:
    """Inside a quoted phrase only the quote and backslash need escaping."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def clean_for_search(value: str) -> str:
    """Strip YouTube-title junk while keeping real words intact."""
    value = unicode_fold(value)
    value = _TOPIC_CHANNEL.sub("", value)
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", value)
    value = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", "", value, flags=re.IGNORECASE)
    value = _SEARCH_JUNK.sub(" ", value)
    value = re.sub(r"\s{2,}", " ", value).strip(" -|")
    return value


def base_title(value: str) -> str:
    """clean_for_search() plus version qualifiers removed."""
    value = _VERSION_QUALIFIER.sub(" ", clean_for_search(value))
    return re.sub(r"\s{2,}", " ", value).strip(" -")


def primary_artist(value: str) -> str:
    """
    Reduce a messy artist credit to its first named artist. Collaboration
    ordering is a very common reason an otherwise-perfect search returns
    nothing at all.
    """
    value = _TOPIC_CHANNEL.sub("", unicode_fold(value))
    value = re.split(
        r"\s*(?:,|&|\bx\b|\bvs\.?\b|\bfeat\.?\b|\bft\.?\b|/)\s*",
        value, flags=re.IGNORECASE,
    )[0]
    return value.strip()


_FEAT_IN_BRACKETS = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]*)[\)\]]", re.IGNORECASE)

# Always a credit separator.
_STRONG_ARTIST_SEP = re.compile(r"\s*;\s*|\s+(?:feat\.?|ft\.?|featuring|vs\.?)\s+", re.IGNORECASE)

# Usually a separator, but also found inside real names ("Simon & Garfunkel",
# "Earth, Wind & Fire", "Tyler, The Creator", "AC/DC"), so it is only trusted
# after checking that the whole string isn't a known MusicBrainz artist.
_WEAK_ARTIST_SEP = re.compile(r"\s*,\s*|\s+&\s+|\s+(?:and|x|with)\s+|\s*/\s*", re.IGNORECASE)


def split_artists(value: str, mb: Optional["MBClient"] = None) -> list[str]:
    """
    "charli xcx & A.G. Cook" -> ["charli xcx", "A.G. Cook"]

    Used for artist strings that don't come with MusicBrainz's own structured
    credit. Without a MusicBrainz client to verify against, only the
    unambiguous separators (feat., ft., vs., ;) are split.
    """
    value = _TOPIC_CHANNEL.sub("", unicode_fold(value))
    value = _FEAT_IN_BRACKETS.sub(r" feat. \1", value)

    names: list[str] = []
    for chunk in _STRONG_ARTIST_SEP.split(value):
        chunk = chunk.strip()
        if not chunk:
            continue
        pieces = [p.strip() for p in _WEAK_ARTIST_SEP.split(chunk) if p.strip()]
        if len(pieces) > 1 and mb is not None and not mb.artist_known(chunk):
            names.extend(pieces)
        else:
            names.append(chunk)

    seen: set[str] = set()
    unique = []
    for name in names:
        key = norm(name)
        if key and key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def split_artist_title(raw: str) -> tuple[str, str]:
    """Pull 'Artist - Title' apart when one field carries both."""
    parts = re.split(r"\s+[-\u2013\u2014]\s+", unicode_fold(raw), maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return "", raw


def _token_sim(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def title_similarity(a: str, b: str) -> float:
    """0.0-1.0 similarity tolerant of extra or reordered words."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    scores = [SequenceMatcher(None, a, b).ratio(), _token_sim(a, b)]
    if a in b or b in a:
        scores.append(0.90)
    return max(scores)


def _text_score(a: str, b: str, exact: float, partial: float, fuzzy: float, floor: float = 0.72) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return exact
    if a in b or b in a:
        return partial
    ratio = SequenceMatcher(None, a, b).ratio()
    return fuzzy * ratio if ratio >= floor else 0.0


def duration_seconds(mp4: MP4) -> Optional[float]:
    try:
        return float(mp4.info.length)
    except Exception:
        return None


def get_first(tags, key: str) -> str:
    if not tags or key not in tags:
        return ""
    value = tags[key]
    if isinstance(value, list):
        return clean_text(value[0]) if value else ""
    return clean_text(value)


def get_all(tags, key: str) -> list[str]:
    """Every value of a multi-value tag, e.g. all contributing artists."""
    if not tags or key not in tags:
        return []
    value = tags[key]
    values = value if isinstance(value, list) else [value]
    return [clean_text(v) for v in values if clean_text(v)]


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    return name.rstrip(" .") or "untitled"


MEDIA_SUFFIXES = {".m4a", ".mp4", ".aac", ".webm", ".opus", ".mp3"}


def media_files(folder: Path) -> set[Path]:
    return {p.resolve() for p in folder.glob("*") if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES}


# ----------------------------- MusicBrainz ----------------------------------


class MBClient:
    def __init__(self, cache_file: Path = CACHE_FILE):
        self.cache_file = cache_file
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.last_request = 0.0
        self.cache: dict[str, Any] = {}
        if cache_file.exists():
            try:
                self.cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                self.cache = {}

    def save_cache(self):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.cache_file)

    def get(self, url: str, params: Optional[dict] = None, cache_key: Optional[str] = None):
        key = cache_key or url + "?" + json.dumps(params or {}, sort_keys=True)
        if key in self.cache:
            return self.cache[key]

        wait = MB_DELAY - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)

        response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        self.last_request = time.monotonic()
        response.raise_for_status()
        data = response.json()
        self.cache[key] = data
        self.save_cache()
        return data

    def _search(self, entity: str, query: str, dismax: bool = False, limit: int = 25) -> dict:
        params = {"query": query, "fmt": "json", "limit": limit}
        if dismax:
            params["dismax"] = "true"
        return self.get(f"{MB_BASE}/{entity}", params)

    def search_recordings_raw(self, query: str, dismax: bool = False) -> list[dict]:
        try:
            return self._search("recording", query, dismax=dismax).get("recordings", [])
        except Exception:
            return []

    def search_releases(self, album: str, artist: str) -> list[dict]:
        parts = [f'release:"{escape_phrase(album)}"']
        if artist:
            parts.append(f'artist:"{escape_phrase(artist)}"')
        try:
            return self._search("release", " AND ".join(parts), limit=10).get("releases", [])
        except Exception:
            return []

    def artist_known(self, name: str) -> bool:
        """
        True if `name` is, as a whole, an artist MusicBrainz knows about
        ("Simon & Garfunkel"), meaning it must not be split into pieces.
        If the lookup fails we answer True, so nothing gets split wrongly.
        """
        target = norm(name)
        if not target:
            return True
        try:
            hits = self._search("artist", f'artist:"{escape_phrase(name)}"', limit=5).get("artists", [])
        except Exception:
            return True
        for hit in hits:
            if norm(hit.get("name", "")) == target:
                return True
            if any(norm(a.get("name", "")) == target for a in hit.get("aliases") or []):
                return True
        return False

    def recording(self, rid: str) -> dict:
        return self.get(
            f"{MB_BASE}/recording/{rid}",
            {"inc": "artists+artist-credits+releases+release-groups+genres+tags", "fmt": "json"},
            cache_key=f"recording:{rid}",
        )

    def release(self, rid: str) -> dict:
        return self.get(
            f"{MB_BASE}/release/{rid}",
            {"inc": "artists+artist-credits+release-groups+media+recordings", "fmt": "json"},
            cache_key=f"release:{rid}",
        )

    def cover(self, release_id: str) -> Optional[bytes]:
        return self._fetch_cover(f"{CAA_BASE}/release/{release_id}/front-1200", f"cover:{release_id}")

    def cover_release_group(self, rg_id: str) -> Optional[bytes]:
        return self._fetch_cover(f"{CAA_BASE}/release-group/{rg_id}/front", f"cover:rg:{rg_id}")

    def _fetch_cover(self, url: str, cache_key: str) -> Optional[bytes]:
        # A cached None means "confirmed missing, don't retry".
        if cache_key in self.cache and self.cache[cache_key] is None:
            return None
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            if response.status_code != 200:
                self.cache[cache_key] = None
                self.save_cache()
                return None
            return response.content
        except requests.RequestException:
            return None


def build_queries(title: str, artist: str) -> list[tuple[str, str, bool]]:
    """
    Progressively looser MusicBrainz queries.

    This cascade is the main fix for "the song is obviously in MusicBrainz but
    the script can't find it". The old code ran exactly one strict quoted
    query and gave up; any stray word, guest credit, or unusual punctuation in
    the YouTube title was enough to return zero rows.

    Returns (label, query, use_dismax).
    """
    ct, ca = clean_for_search(title), clean_for_search(artist)
    bt, pa = base_title(title), primary_artist(artist)
    at, aa = strip_accents(bt), strip_accents(pa)

    queries: list[tuple[str, str, bool]] = []
    seen: set[str] = set()

    def add(label: str, query: str, dismax: bool = False):
        query = query.strip()
        if query and query not in seen:
            seen.add(query)
            queries.append((label, query, dismax))

    def phrase(t: str, a: str) -> str:
        parts = []
        if t:
            parts.append(f'recording:"{escape_phrase(t)}"')
        if a:
            parts.append(f'artist:"{escape_phrase(a)}"')
        return " AND ".join(parts)

    # 1. Strict: exact phrases for both fields.
    add("exact title + artist", phrase(ct, ca))
    # 2. Version qualifiers removed ("(Sped Up)", "(Remastered 2011)", ...).
    if bt != ct or pa != ca:
        add("base title + primary artist", phrase(bt, pa))
    # 3. Unquoted tokens: matches when word order or spacing differs.
    if bt:
        tokens = " AND ".join(escape_lucene(w) for w in bt.split())
        tail = f' AND artist:"{escape_phrase(pa)}"' if pa else ""
        add("loose tokens + artist", f"recording:({tokens}){tail}")
    # 4. Accent-folded, for titles MusicBrainz stores without diacritics.
    if at != bt or aa != pa:
        add("accent-folded", phrase(at, aa))
    # 5. Title only. The artist tag is the noisiest field on a YouTube
    #    download ("- Topic" channels, "Various Artists", guest credits).
    add("title only", phrase(bt, ""))
    # 6. dismax: Solr's forgiving parser, plain text, no field syntax.
    add("dismax title + artist", f"{bt} {pa}".strip(), True)
    add("dismax title only", bt, True)

    return queries


def artist_credit_name(obj: dict) -> str:
    if obj.get("artist-credit"):
        parts = []
        for credit in obj["artist-credit"]:
            parts.append(credit.get("artist", {}).get("name", ""))
            parts.append(credit.get("joinphrase", ""))
        return "".join(parts).strip()
    return ""


def artist_credit_names(obj: dict) -> list[str]:
    """MusicBrainz already splits a credit into individual artists; use that."""
    names: list[str] = []
    for credit in obj.get("artist-credit") or []:
        name = clean_text((credit.get("artist") or {}).get("name") or credit.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def release_group_type(rg: dict) -> str:
    return clean_text(rg.get("primary-type")).casefold()


def score_recording(rec: dict, title: str, artist: str, album: str, dur: Optional[float]) -> float:
    score = _text_score(norm(rec.get("title", "")), norm(title), 55, 35, 30)
    score += _text_score(norm(artist_credit_name(rec)), norm(artist), 35, 20, 18)

    target_alb = norm(album)
    if target_alb:
        for rel in rec.get("releases") or []:
            rr = norm(rel.get("title", ""))
            if rr == target_alb:
                score += 30
                break
            if rr and (rr in target_alb or target_alb in rr):
                score += 12
                break

    length = rec.get("length")
    if dur and length:
        try:
            diff = abs(dur * 1000 - float(length))
        except (TypeError, ValueError):
            diff = None
        if diff is not None:
            if diff <= 2000:
                score += 15
            elif diff <= 5000:
                score += 10
            elif diff <= 10000:
                score += 5
    return score


def _year_of(date_str: str) -> int:
    m = re.match(r"(\d{4})", date_str or "")
    return int(m.group(1)) if m else 9999


def choose_release(rec: dict, album: str, preferred_ids: Optional[set] = None) -> Optional[dict]:
    releases = rec.get("releases") or []
    if not releases:
        return None
    target = norm(album)
    preferred_ids = preferred_ids or set()

    def sort_key(rel):
        # Top priority: a release already confirmed by a sibling track in this
        # same run, so a playlist drawn from one album stays on that album.
        pooled = 1 if rel.get("id") in preferred_ids else 0
        exact = 1 if target and norm(rel.get("title", "")) == target else 0
        rg_type = release_group_type(rel.get("release-group", {}))
        preferred_type = 1 if rg_type in {"album", "ep", "single"} else 0
        official = 1 if clean_text(rel.get("status")).casefold() == "official" else 0
        earliness = -_year_of(clean_text(rel.get("date")))
        return (pooled, exact, preferred_type, official, earliness)

    return sorted(releases, key=sort_key, reverse=True)[0]


def tracklist(release: dict) -> list[dict]:
    """Flatten a fully-fetched release into comparable track entries."""
    media = release.get("media") or []
    total_tracks = sum(len(m.get("tracks") or []) for m in media)
    entries = []
    for disc_index, medium in enumerate(media, start=1):
        tracks = medium.get("tracks") or []
        for track_index, track in enumerate(tracks, start=1):
            rec = track.get("recording") or {}
            entries.append({
                "title": clean_text(track.get("title") or rec.get("title")),
                "recording_id": rec.get("id", ""),
                "track_no": track.get("position") or track_index,
                "track_total": len(tracks),
                "disc_no": medium.get("position") or disc_index,
                "disc_total": len(media),
                "is_multi": total_tracks > 1,
            })
    return entries


def find_recording_position(release: dict, recording_id: str):
    """Returns track_no, track_total, disc_no, disc_total, is_multi."""
    entries = tracklist(release)
    if not entries:
        return None, None, None, None, False
    is_multi = entries[0]["is_multi"]
    for e in entries:
        if e["recording_id"] == recording_id:
            return e["track_no"], e["track_total"], e["disc_no"], e["disc_total"], e["is_multi"]
    return None, None, None, None, is_multi


def match_title_in_tracklist(title: str, entries: list[dict], threshold: float):
    best, best_score = None, 0.0
    for entry in entries:
        score = title_similarity(title, entry["title"])
        if score > best_score:
            best, best_score = entry, score
    if best and best_score >= threshold:
        return best, best_score
    return None


# ----------------------------- TheAudioDB (fallback) ------------------------


class AudioDBClient:
    """Shares the MBClient cache so everything stays in one JSON file."""

    def __init__(self, cache: dict, save_cache, api_key: str = ADB_API_KEY):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.last_request = 0.0
        self.cache = cache
        self.save_cache = save_cache
        self.api_key = api_key or ADB_API_KEY

    def _get(self, path: str, params: dict, cache_key: str) -> dict:
        if cache_key in self.cache:
            return self.cache[cache_key]
        wait = ADB_DELAY - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        response = self.session.get(f"{ADB_BASE}/{self.api_key}/{path}", params=params, timeout=REQUEST_TIMEOUT)
        self.last_request = time.monotonic()
        response.raise_for_status()
        data = response.json()
        self.cache[cache_key] = data
        self.save_cache()
        return data

    def search_track(self, artist: str, title: str) -> list[dict]:
        if not artist or not title:
            return []
        data = self._get("searchtrack.php", {"s": artist, "t": title},
                         cache_key=f"adb:searchtrack:{norm(artist)}|{norm(title)}")
        return data.get("track") or []

    def track_by_id(self, track_id: str) -> Optional[dict]:
        tracks = self._get("track.php", {"h": track_id}, cache_key=f"adb:track:{track_id}").get("track") or []
        return tracks[0] if tracks else None

    def album_tracks(self, album_id: str) -> list[dict]:
        return self._get("track.php", {"m": album_id}, cache_key=f"adb:albumtracks:{album_id}").get("track") or []

    def album_by_id(self, album_id: str) -> Optional[dict]:
        albums = self._get("album.php", {"m": album_id}, cache_key=f"adb:album:{album_id}").get("album") or []
        return albums[0] if albums else None

    def fetch_image(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
            return response.content if response.status_code == 200 else None
        except requests.RequestException:
            return None


def score_audiodb_track(track: dict, title: str, artist: str, album: str, dur: Optional[float]) -> float:
    score = _text_score(norm(track.get("strTrack") or ""), norm(title), 55, 35, 30)
    score += _text_score(norm(track.get("strArtist") or ""), norm(artist), 35, 20, 18)

    ralb, target_alb = norm(track.get("strAlbum") or ""), norm(album)
    if target_alb and ralb:
        if ralb == target_alb:
            score += 30
        elif ralb in target_alb or target_alb in ralb:
            score += 12

    length = track.get("intDuration")
    if dur and length:
        try:
            diff = abs(dur * 1000 - float(length))
        except (TypeError, ValueError):
            diff = None
        if diff is not None:
            if diff <= 2000:
                score += 15
            elif diff <= 5000:
                score += 10
            elif diff <= 10000:
                score += 5
    return score


def _as_int(value) -> Optional[int]:
    try:
        if value is None or clean_text(value) == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ----------------------------- AcoustID (fingerprinting) --------------------


class AcoustIDClient:
    """
    Identifies a file by what it actually sounds like rather than by its
    title. This is the only reliable way to match a track whose YouTube title
    bears no useful resemblance to its real one.

    Needs the `fpcalc` binary (Chromaprint) on PATH and a free API key from
    https://acoustid.org/new-application
    """

    def __init__(self, api_key: str, cache: dict, save_cache):
        self.api_key = api_key
        self.cache = cache
        self.save_cache = save_cache
        self.last_request = 0.0
        self.available = bool(api_key) and shutil.which("fpcalc") is not None

    def fingerprint(self, path: Path) -> Optional[tuple]:
        try:
            result = subprocess.run(
                ["fpcalc", "-json", str(path)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            duration, fp = data.get("duration"), data.get("fingerprint")
            if not duration or not fp:
                return None
            return int(float(duration)), fp
        except Exception:
            return None

    def lookup(self, path: Path) -> list[dict]:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        cache_key = f"acoustid:file:{path.name}:{size}"
        if cache_key in self.cache:
            return self.cache[cache_key] or []

        printed = self.fingerprint(path)
        if not printed:
            return []
        duration, fp = printed

        wait = ACOUSTID_DELAY - (time.monotonic() - self.last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = requests.post(
                ACOUSTID_BASE,
                data={
                    "client": self.api_key,
                    "duration": duration,
                    "fingerprint": fp,
                    "meta": "recordings releaseids",
                    "format": "json",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            self.last_request = time.monotonic()
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            print(f"  AcoustID lookup failed: {exc}")
            return []

        if data.get("status") != "ok":
            return []
        results = data.get("results") or []
        self.cache[cache_key] = results
        self.save_cache()
        return results

    def best_recording_id(self, path: Path, title: str, artist: str) -> tuple:
        """Pick the fingerprint result whose recording best fits our tags."""
        best_id, best_rank = "", 0.0
        for result in self.lookup(path):
            try:
                confidence = float(result.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if confidence < 0.5:
                continue
            for rec in result.get("recordings") or []:
                rid = rec.get("id")
                if not rid:
                    continue
                names = [a.get("name", "") for a in rec.get("artists") or []]
                text_fit = max(
                    title_similarity(title, rec.get("title") or ""),
                    max((title_similarity(artist, n) for n in names), default=0.0),
                )
                # Weight the acoustic score heavily; text is only a tiebreak
                # between the several MusicBrainz recordings one fingerprint
                # can legitimately map to (album vs single vs compilation).
                rank = confidence + 0.25 * text_fit
                if rank > best_rank:
                    best_id, best_rank = rid, rank
        return best_id, best_rank


# ----------------------------- lyrics ----------------------------------------


@dataclass
class LyricsResult:
    text: str
    source: str
    partial: bool = False


_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "wbr", "source"}


class _GeniusLyricsParser(HTMLParser):
    """Collects the text inside Genius's lyrics containers, keeping line breaks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0       # nesting depth inside a lyrics container
        self.skip_depth = 0  # nesting depth inside an excluded block (ads, headers)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if self.depth == 0:
            if tag == "div" and attr.get("data-lyrics-container") == "true":
                self.depth = 1
            return
        if tag == "br":
            if not self.skip_depth:
                self.parts.append("\n")
            return
        if tag in _VOID_TAGS:
            return
        self.depth += 1
        if self.skip_depth:
            self.skip_depth += 1
        elif attr.get("data-exclude-from-selection") == "true":
            self.skip_depth = 1

    def handle_endtag(self, tag):
        if self.depth == 0 or tag in _VOID_TAGS:
            return
        self.depth -= 1
        if self.skip_depth:
            self.skip_depth -= 1
        if self.depth == 0:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.depth and not self.skip_depth:
            self.parts.append(data)


def _strict_title_sim(a: str, b: str) -> float:
    """Whole-string similarity. Unlike title_similarity, 'Home' != 'Home Again'."""
    a, b = norm(a), norm(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


class LyricsClient:
    """
    Musixmatch first, Genius as the fallback.

    Musixmatch: official API (matcher.lyrics.get). Its free plan returns only
    about 30% of each song, flagged in lyrics_copyright, so a preview like that
    is treated as "not found" and Genius gets a turn.

    Genius: its API only searches; it does not return lyrics. The text comes
    from reading the song's public page, which is fragile (the page layout can
    change or block automated requests) and is not something Genius's terms
    welcome. Treat it as best-effort.
    """

    BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )

    def __init__(self, musixmatch_key: str, genius_token: str, allow_partial: bool = False):
        self.musixmatch_key = clean_text(musixmatch_key)
        self.genius_token = clean_text(genius_token)
        self.allow_partial = allow_partial
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.mxm_disabled = False
        self.genius_disabled = False
        self.saw_partial = False
        self._last_mxm = 0.0
        self._last_genius = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.musixmatch_key or self.genius_token)

    @property
    def signature(self) -> str:
        """Which sources are configured, so a cached 'not found' is retried
        once a new source (say a Genius token) has been added."""
        return ("m" if self.musixmatch_key else "") + ("g" if self.genius_token else "")

    # ------------------------------------------------------------ Musixmatch

    @staticmethod
    def _strip_musixmatch_footer(text: str) -> str:
        return re.sub(r"\s*\*{3,}[^*]*\*{3,}\s*(?:\(\d+\))?\s*$", "", text).strip()

    def _musixmatch(self, artist: str, title: str) -> Optional[LyricsResult]:
        if not self.musixmatch_key or self.mxm_disabled:
            return None

        for query_title in dict.fromkeys(t for t in (base_title(title), clean_for_search(title)) if t):
            wait = MUSIXMATCH_DELAY - (time.monotonic() - self._last_mxm)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(
                    f"{MUSIXMATCH_BASE}/matcher.lyrics.get",
                    params={"q_track": query_title, "q_artist": artist,
                            "apikey": self.musixmatch_key, "format": "json"},
                    timeout=REQUEST_TIMEOUT,
                )
                self._last_mxm = time.monotonic()
                message = response.json().get("message", {})
            except Exception as exc:
                print(f"  Musixmatch request failed: {exc}")
                return None

            status = (message.get("header") or {}).get("status_code")
            if status in (401, 402, 403):
                print(f"  Musixmatch refused the request (status {status}): bad key or daily limit reached.")
                self.mxm_disabled = True
                return None
            if status != 200:
                continue

            body = message.get("body")
            lyrics = body.get("lyrics") if isinstance(body, dict) else None
            if not lyrics:
                continue
            text = self._strip_musixmatch_footer(clean_text(lyrics.get("lyrics_body")))
            if not text:
                continue
            partial = bool(re.search(r"only\s+\d+\s*%", clean_text(lyrics.get("lyrics_copyright")), re.IGNORECASE))
            return LyricsResult(text, "musixmatch", partial)
        return None

    # ----------------------------------------------------------------- Genius

    def _genius_wait(self):
        wait = GENIUS_DELAY - (time.monotonic() - self._last_genius)
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _pick_genius_hit(hits: list, artist: str, title: str) -> str:
        best_url, best_score = "", 0.0
        for hit in hits:
            if hit.get("type") not in (None, "song"):
                continue
            result = hit.get("result") or {}
            url = clean_text(result.get("url"))
            primary = clean_text((result.get("primary_artist") or {}).get("name"))
            if not url or primary.casefold().startswith("genius "):
                continue  # Genius's own translation / romanization pages
            t_sim = _strict_title_sim(title, result.get("title") or "")
            a_sim = max(
                _strict_title_sim(artist, primary),
                title_similarity(artist, result.get("artist_names") or ""),
            )
            if t_sim < 0.85 or a_sim < 0.5:
                continue
            score = 0.7 * t_sim + 0.3 * a_sim
            if score > best_score:
                best_url, best_score = url, score
        return best_url

    @staticmethod
    def _parse_genius_page(html: str) -> str:
        parser = _GeniusLyricsParser()
        parser.feed(html)
        text = "".join(parser.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"(?m)^\s*You might also like\s*$\n?", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        text = re.sub(r"\d*Embed\s*$", "", text).strip()
        if len(text) < 20 or re.search(r"lyrics for this song have yet to be released", text, re.IGNORECASE):
            return ""
        return text

    def _genius(self, artist: str, title: str) -> Optional[LyricsResult]:
        if not self.genius_token or self.genius_disabled:
            return None

        query_title = base_title(title) or title
        self._genius_wait()
        try:
            response = self.session.get(
                f"{GENIUS_API_BASE}/search",
                params={"q": f"{query_title} {artist}".strip()},
                headers={"Authorization": f"Bearer {self.genius_token}"},
                timeout=REQUEST_TIMEOUT,
            )
            self._last_genius = time.monotonic()
            if response.status_code == 401:
                print("  Genius rejected the access token.")
                self.genius_disabled = True
                return None
            hits = (response.json().get("response") or {}).get("hits") or []
        except Exception as exc:
            print(f"  Genius search failed: {exc}")
            return None

        url = self._pick_genius_hit(hits, artist, query_title)
        if not url:
            return None

        self._genius_wait()
        try:
            page = self.session.get(url, headers={"User-Agent": self.BROWSER_UA}, timeout=REQUEST_TIMEOUT)
            self._last_genius = time.monotonic()
            if page.status_code != 200:
                return None
            text = self._parse_genius_page(page.text)
        except Exception as exc:
            print(f"  Genius page fetch failed: {exc}")
            return None
        return LyricsResult(text, "genius") if text else None

    # ------------------------------------------------------------------ main

    def fetch(self, artist: str, title: str) -> Optional[LyricsResult]:
        mxm = self._musixmatch(artist, title)
        if mxm and not mxm.partial:
            return mxm
        genius = self._genius(artist, title)
        if genius:
            return genius
        if mxm and mxm.partial:
            if self.allow_partial:
                return mxm
            self.saw_partial = True
        return None


# ----------------------------- match model -----------------------------------


@dataclass
class TagMatch:
    source: str
    title: str
    artist: str
    album: str = ""
    track_no: Optional[int] = None
    track_total: Optional[int] = None
    disc_no: Optional[int] = None
    disc_total: Optional[int] = None
    is_multi: bool = False
    date: str = ""
    genres: list = field(default_factory=list)
    cover_bytes: Optional[bytes] = None
    mb_recording_id: str = ""
    mb_release_id: str = ""
    mb_release_group_id: str = ""
    adb_track_id: str = ""
    release_desc: str = ""
    via: str = "text"
    artists: list = field(default_factory=list)  # individual contributing artists


@dataclass
class FileInfo:
    path: Path
    title: str
    artist: str
    album: str
    dur: Optional[float]

    @property
    def key(self) -> str:
        return f"{norm(self.artist)}|{norm(self.title)}|{norm(self.album)}|{round(self.dur or 0)}"


@dataclass
class Context:
    mb: MBClient
    adb: Optional[AudioDBClient]
    acoustid: Optional[AcoustIDClient]
    auto_accept: bool
    pick: bool
    force: bool
    rename: bool
    overrides: dict
    # release_id -> fully-fetched release JSON, for album-context matching
    release_pool: dict = field(default_factory=dict)
    confirmed_ids: set = field(default_factory=set)
    lyrics: Optional[LyricsClient] = None
    relyrics: bool = False


def build_match_from_musicbrainz(rec: dict, release: Optional[dict], mb: MBClient, via: str = "text") -> TagMatch:
    title = clean_text(rec.get("title"))
    artist = artist_credit_name(rec)
    release_title = clean_text(release.get("title")) if release else ""
    rg = (release.get("release-group") or {}) if release else {}
    rg_type = release_group_type(rg)

    track_no = track_total = disc_no = disc_total = None
    is_multi = False
    cover_bytes = None
    date = ""

    if release:
        full = mb.release(release["id"])
        track_no, track_total, disc_no, disc_total, is_multi = find_recording_position(full, rec["id"])
        release_title = clean_text(full.get("title")) or release_title
        rg = full.get("release-group") or rg
        rg_type = release_group_type(rg)
        date = clean_text(full.get("date")) or clean_text(release.get("date"))
        cover_bytes = mb.cover(release["id"])
        if not cover_bytes and rg.get("id"):
            cover_bytes = mb.cover_release_group(rg["id"])

    genres = [g.get("name") for g in rec.get("genres", []) if g.get("name")]
    if not genres:
        genres = [t.get("name") for t in rec.get("tags", []) if t.get("name")]
    genres = list(dict.fromkeys(g for g in genres if g))

    release_desc = release_title or "no release"
    if rg_type:
        release_desc += f" [{rg_type}]"

    return TagMatch(
        source="musicbrainz", title=title, artist=artist, album=release_title,
        artists=artist_credit_names(rec),
        track_no=track_no, track_total=track_total, disc_no=disc_no, disc_total=disc_total,
        is_multi=is_multi, date=date, genres=genres, cover_bytes=cover_bytes,
        mb_recording_id=rec.get("id", ""),
        mb_release_id=release.get("id", "") if release else "",
        mb_release_group_id=rg.get("id", ""), release_desc=release_desc, via=via,
    )


def build_match_from_tracklist_entry(entry: dict, release: dict, mb: MBClient, via: str) -> TagMatch:
    """
    Build a match from a known position inside a known release. Used by
    album-context matching, where the release is already trusted and all that
    was needed was to locate the track within it.
    """
    if entry.get("recording_id"):
        try:
            rec = mb.recording(entry["recording_id"])
            return build_match_from_musicbrainz(rec, release, mb, via=via)
        except Exception:
            pass

    # Fall back to the release/track data already in hand.
    rg = release.get("release-group") or {}
    cover_bytes = mb.cover(release["id"])
    if not cover_bytes and rg.get("id"):
        cover_bytes = mb.cover_release_group(rg["id"])
    return TagMatch(
        source="musicbrainz", title=entry["title"], artist=artist_credit_name(release),
        artists=artist_credit_names(release),
        album=clean_text(release.get("title")), track_no=entry["track_no"],
        track_total=entry["track_total"], disc_no=entry["disc_no"], disc_total=entry["disc_total"],
        is_multi=entry["is_multi"], date=clean_text(release.get("date")),
        cover_bytes=cover_bytes, mb_release_id=release.get("id", ""),
        mb_release_group_id=rg.get("id", ""),
        release_desc=f"{clean_text(release.get('title'))} [{release_group_type(rg) or 'release'}]", via=via,
    )


def build_match_from_audiodb(track: dict, adb: AudioDBClient) -> TagMatch:
    album_id = track.get("idAlbum")
    track_total, is_multi, album_obj = None, False, None
    if album_id:
        try:
            tracks = adb.album_tracks(album_id)
            track_total = len(tracks) if tracks else None
            is_multi = bool(track_total and track_total > 1)
        except Exception:
            pass
        try:
            album_obj = adb.album_by_id(album_id)
        except Exception:
            album_obj = None

    cover_url = clean_text(album_obj.get("strAlbumThumb")) if album_obj else ""
    if not cover_url:
        cover_url = clean_text(track.get("strTrackThumb"))
    album = clean_text(track.get("strAlbum"))
    genre = clean_text(track.get("strGenre"))

    return TagMatch(
        source="theaudiodb", title=clean_text(track.get("strTrack")),
        artist=clean_text(track.get("strArtist")), album=album,
        track_no=_as_int(track.get("intTrackNumber")), track_total=track_total,
        disc_no=_as_int(track.get("intCD")), disc_total=None, is_multi=is_multi,
        date=clean_text(album_obj.get("intYearReleased")) if album_obj else "",
        genres=[genre] if genre else [],
        cover_bytes=adb.fetch_image(cover_url) if cover_url else None,
        adb_track_id=clean_text(track.get("idTrack")),
        release_desc=(album or "no release") + " [TheAudioDB]",
    )


# ----------------------------- tagging --------------------------------------


def make_square_cover(raw: bytes) -> bytes:
    from io import BytesIO
    from PIL import Image

    with Image.open(BytesIO(raw)) as im:
        im = im.convert("RGB")
        side = min(im.width, im.height)
        left, top = (im.width - side) // 2, (im.height - side) // 2
        im = im.crop((left, top, left + side, top + side))
        im = im.resize((1200, 1200), Image.Resampling.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=92, optimize=True)
        return out.getvalue()


def square_existing_cover(mp4) -> bool:
    """Crop the embedded cover to a centered square if it isn't one already."""
    covers = mp4.tags.get("covr") if mp4.tags is not None else None
    if not covers:
        return False
    try:
        from io import BytesIO
        from PIL import Image

        raw = bytes(covers[0])
        with Image.open(BytesIO(raw)) as im:
            if im.width == im.height:
                return False
        mp4.tags["covr"] = [MP4Cover(make_square_cover(raw), imageformat=MP4Cover.FORMAT_JPEG)]
        return True
    except Exception as exc:
        print(f"    Cover art warning: {exc}")
        return False


def apply_match_tags(path: Path, match: TagMatch, album_hint: str, rename: bool,
                     splitter: Optional[Callable[[str], list]] = None) -> tuple:
    mp4 = MP4(str(path))
    if mp4.tags is None:
        mp4.add_tags()

    title = match.title or get_first(mp4.tags, "\xa9nam") or path.stem
    credit = match.artist or " & ".join(get_all(mp4.tags, "\xa9ART"))

    # Separate contributing artists. MusicBrainz credits arrive already split;
    # anything else is split by name.
    artists = list(match.artists)
    if not artists and credit:
        artists = splitter(credit) if splitter else [credit]
    artists = artists or ([credit] if credit else [])
    artist = credit or " & ".join(artists)
    album = match.album or album_hint

    mp4.tags["\xa9nam"] = [title]
    if artists:
        mp4.tags["\xa9ART"] = [ARTIST_SEPARATOR.join(artists)]  # one field: "A / B"
        mp4.tags["aART"] = [artists[0]]    # album artist: the primary artist only
        if len(artists) > 1:
            mp4.tags["----:com.apple.iTunes:ARTISTS"] = [a.encode() for a in artists]
        else:
            mp4.tags.pop("----:com.apple.iTunes:ARTISTS", None)
    if album:
        mp4.tags["\xa9alb"] = [album]

    # Real position inside a multi-track release; otherwise Track 1 of 1.
    if match.is_multi and match.track_no:
        mp4.tags["trkn"] = [(int(match.track_no), int(match.track_total or 0))]
        mp4.tags["disk"] = [(int(match.disc_no or 1), int(match.disc_total or 1))]
    else:
        mp4.tags["trkn"] = [(1, 1)]
        mp4.tags["disk"] = [(1, 1)]

    if match.date:
        mp4.tags["\xa9day"] = [match.date]
    if match.genres:
        mp4.tags["\xa9gen"] = [", ".join(dict.fromkeys(match.genres))]

    try:
        ids = {
            "MusicBrainz Recording Id": match.mb_recording_id,
            "MusicBrainz Release Id": match.mb_release_id,
            "MusicBrainz Release Group Id": match.mb_release_group_id,
            "TheAudioDB Track Id": match.adb_track_id,
        }
        for label, value in ids.items():
            if value:
                mp4.tags[f"----:com.apple.iTunes:{label}"] = [value.encode()]
    except Exception:
        pass

    # Always end up with a square cover. Online art is cropped and resized to
    # 1200x1200; otherwise the thumbnail yt-dlp embedded is cropped to square.
    if match.cover_bytes:
        try:
            mp4.tags["covr"] = [MP4Cover(make_square_cover(match.cover_bytes), imageformat=MP4Cover.FORMAT_JPEG)]
        except Exception as exc:
            print(f"    Cover art warning: {exc}")
            square_existing_cover(mp4)
    else:
        square_existing_cover(mp4)

    mp4.save()

    new_path = path
    if rename:
        candidate = path.with_name(safe_filename(f"{artist} - {title}") + path.suffix.lower())
        if candidate.resolve() != path.resolve():
            if candidate.exists():
                base, i = candidate.stem, 2
                while candidate.exists():
                    candidate = path.with_name(f"{base} ({i}){path.suffix.lower()}")
                    i += 1
            path.rename(candidate)
            new_path = candidate

    return new_path, match.release_desc


# ----------------------------- match resolution ------------------------------


def _confirm(label: str, score: float, release_desc: str, auto_accept: bool) -> Optional[bool]:
    """True = use it, False = reject (try next source), None = skip file."""
    print(f"  Match score: {score:.0f}/135   Release: {release_desc}")
    if score >= WEAK_MATCH_THRESHOLD or auto_accept:
        return True
    answer = input(f"  Use this {label} match? [Y/n/s] ").strip().lower()
    if answer == "s":
        return None
    return answer in {"", "y", "yes"}


def _pick_candidate(candidates: list, info: FileInfo) -> Optional[dict]:
    """Show a numbered candidate list so a near-miss can be rescued by hand."""
    print("  Candidates:")
    shown = candidates[:8]
    for i, rec in enumerate(shown, start=1):
        rels = rec.get("releases") or []
        rel = clean_text(rels[0].get("title")) if rels else "(no release)"
        score = score_recording(rec, info.title, info.artist, info.album, info.dur)
        print(f"    {i}. {artist_credit_name(rec)} - {clean_text(rec.get('title'))}  [{rel}]  ({score:.0f})")
    answer = input("  Pick a number, or Enter to skip picking: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(shown):
        return shown[int(answer) - 1]
    return None


def resolve_from_musicbrainz(info: FileInfo, ctx: Context):
    """Returns (match, cache_entry, explicit_skip)."""
    mb = ctx.mb
    results: list = []
    used_label = ""

    for label, query, dismax in build_queries(info.title, info.artist):
        found = mb.search_recordings_raw(query, dismax=dismax)
        if found:
            results, used_label = found, label
            break

    if not results:
        print("  MusicBrainz: no candidates from any query variant.")
        return None, None, False

    ranked = sorted(
        results,
        key=lambda r: score_recording(r, info.title, info.artist, info.album, info.dur),
        reverse=True,
    )
    top = ranked[0]
    top_score = score_recording(top, info.title, info.artist, info.album, info.dur)

    if ctx.pick and top_score < WEAK_MATCH_THRESHOLD and not ctx.auto_accept:
        chosen = _pick_candidate(ranked, info)
        if chosen is not None:
            top = chosen
            top_score = WEAK_MATCH_THRESHOLD  # user-chosen, treat as confirmed

    try:
        rec = mb.recording(top["id"])
    except Exception as exc:
        print(f"  MusicBrainz lookup failed: {exc}")
        return None, None, False

    release = choose_release(rec, info.album, ctx.confirmed_ids)
    print(f"  MusicBrainz ({used_label}): {artist_credit_name(rec)} - {clean_text(rec.get('title'))}")

    decision = _confirm("MusicBrainz", top_score, clean_text(release.get("title")) if release else "(none)", ctx.auto_accept)
    if decision is None:
        return None, None, True
    if decision is False:
        return None, None, False

    match = build_match_from_musicbrainz(rec, release, mb, via="text")
    entry = {"source": "musicbrainz", "recording_id": rec["id"],
             "release_id": release["id"] if release else None, "via": "text"}
    return match, entry, False


def candidate_releases(info: FileInfo, ctx: Context, search_album: bool) -> Iterable:
    """
    Releases worth checking a stubborn title against, best first: releases
    already confirmed in this run whose title matches the album tag, then any
    other release confirmed in this run, then a fresh album search.
    """
    seen = set()
    target = norm(info.album)
    pooled = list(ctx.release_pool.values())

    if target:
        for rel in pooled:
            if norm(rel.get("title", "")) == target and rel["id"] not in seen:
                seen.add(rel["id"])
                yield rel
    for rel in pooled:
        if rel["id"] not in seen:
            seen.add(rel["id"])
            yield rel

    if search_album and info.album:
        artist = primary_artist(info.artist)
        for hit in ctx.mb.search_releases(clean_for_search(info.album), artist)[:5]:
            rid = hit.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            try:
                yield ctx.mb.release(rid)
            except Exception:
                continue


def resolve_from_album_context(info: FileInfo, ctx: Context, search_album: bool = True):
    """
    The fix for 'every other song on this album matched, but not this one'.

    Rather than searching all of MusicBrainz for a title that evidently
    doesn't index well, compare it against the handful of track titles on a
    release we already trust. With only ~12 candidates, a much looser
    similarity threshold is safe, so guest credits, "(Sped Up)", and
    punctuation differences stop mattering.
    """
    for release in candidate_releases(info, ctx, search_album):
        entries = tracklist(release)
        if not entries or not entries[0]["is_multi"]:
            continue
        # A release confirmed by a sibling track this run is trusted more than
        # one merely guessed at from the album tag.
        threshold = TRACKLIST_SIMILARITY if release.get("id") in ctx.confirmed_ids else TRACKLIST_SIMILARITY_STRICT
        hit = match_title_in_tracklist(info.title, entries, threshold)
        if not hit:
            continue
        entry, score = hit

        rel_title = clean_text(release.get("title"))
        print(f"  Album context: '{entry['title']}' on '{rel_title}' "
              f"(track {entry['track_no']}/{entry['track_total']}, similarity {score:.2f})")

        if score < 0.80 and not ctx.auto_accept:
            answer = input("  Use this album-context match? [Y/n/s] ").strip().lower()
            if answer == "s":
                return None, None, True
            if answer not in {"", "y", "yes"}:
                continue

        match = build_match_from_tracklist_entry(entry, release, ctx.mb, via="album-context")
        cache_entry = {"source": "musicbrainz", "recording_id": entry.get("recording_id") or None,
                       "release_id": release.get("id"), "via": "album-context"}
        return match, cache_entry, False

    return None, None, False


def resolve_from_acoustid(info: FileInfo, ctx: Context):
    acoustid = ctx.acoustid
    if acoustid is None or not acoustid.available:
        return None, None, False

    print("  Fingerprinting audio (AcoustID)...")
    rid, rank = acoustid.best_recording_id(info.path, info.title, info.artist)
    if not rid:
        print("  AcoustID: no confident fingerprint match.")
        return None, None, False

    try:
        rec = ctx.mb.recording(rid)
    except Exception as exc:
        print(f"  AcoustID -> MusicBrainz lookup failed: {exc}")
        return None, None, False

    release = choose_release(rec, info.album, ctx.confirmed_ids)
    print(f"  AcoustID: {artist_credit_name(rec)} - {clean_text(rec.get('title'))} (confidence {rank:.2f})")

    if rank < 0.85 and not ctx.auto_accept:
        answer = input("  Use this fingerprint match? [Y/n/s] ").strip().lower()
        if answer == "s":
            return None, None, True
        if answer not in {"", "y", "yes"}:
            return None, None, False

    match = build_match_from_musicbrainz(rec, release, ctx.mb, via="acoustid")
    entry = {"source": "musicbrainz", "recording_id": rid,
             "release_id": release["id"] if release else None, "via": "acoustid"}
    return match, entry, False


def resolve_from_audiodb(info: FileInfo, ctx: Context):
    adb = ctx.adb
    if adb is None:
        return None, None, False
    try:
        candidates = adb.search_track(primary_artist(info.artist), base_title(info.title))
    except Exception as exc:
        print(f"  TheAudioDB search failed: {exc}")
        return None, None, False

    if not candidates:
        print("  TheAudioDB: no candidates found.")
        return None, None, False

    ranked = sorted(
        candidates,
        key=lambda t: score_audiodb_track(t, info.title, info.artist, info.album, info.dur),
        reverse=True,
    )
    top = ranked[0]
    top_score = score_audiodb_track(top, info.title, info.artist, info.album, info.dur)

    print(f"  TheAudioDB: {clean_text(top.get('strArtist'))} - {clean_text(top.get('strTrack'))}")
    decision = _confirm("TheAudioDB", top_score, clean_text(top.get("strAlbum")) or "(none)", ctx.auto_accept)
    if decision is None:
        return None, None, True
    if decision is False:
        return None, None, False

    entry = {"source": "theaudiodb", "track_id": clean_text(top.get("idTrack"))}
    return build_match_from_audiodb(top, adb), entry, False


# ----------------------------- overrides -------------------------------------


_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def extract_mbid(value: str) -> str:
    m = _MBID.search(clean_text(value))
    return m.group(0) if m else ""


def load_overrides(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read {path.name}: {exc}")
        return {}

    overrides = {}
    for key, value in raw.items():
        if isinstance(value, str):
            value = {"recording": value}
        if not isinstance(value, dict):
            continue
        overrides[norm(key)] = {
            "recording": extract_mbid(value.get("recording", "")),
            "release": extract_mbid(value.get("release", "")),
        }
    return overrides


def lookup_override(info: FileInfo, overrides: dict) -> Optional[dict]:
    for key in (info.path.name, info.path.stem, f"{info.artist} - {info.title}", info.title):
        hit = overrides.get(norm(key))
        if hit:
            return hit
    return None


def resolve_from_override(info: FileInfo, ctx: Context):
    hit = lookup_override(info, ctx.overrides)
    if not hit:
        return None, None, False

    rec_id, rel_id = hit.get("recording"), hit.get("release")

    # A release-only override pins the album; the track is then located inside
    # that album's tracklist by title.
    if not rec_id and rel_id:
        try:
            release = ctx.mb.release(rel_id)
        except Exception as exc:
            print(f"  Override release lookup failed: {exc}")
            return None, None, False
        found = match_title_in_tracklist(info.title, tracklist(release), 0.45)
        if not found:
            print("  Override release found, but no track title was close enough.")
            return None, None, False
        entry, _ = found
        print(f"  Override: '{entry['title']}' on '{clean_text(release.get('title'))}'")
        cache_entry = {"source": "musicbrainz", "recording_id": entry.get("recording_id") or None,
                       "release_id": rel_id, "via": "override"}
        return build_match_from_tracklist_entry(entry, release, ctx.mb, via="override"), cache_entry, False

    if not rec_id:
        return None, None, False

    try:
        rec = ctx.mb.recording(rec_id)
    except Exception as exc:
        print(f"  Override recording lookup failed: {exc}")
        return None, None, False

    release = None
    if rel_id:
        try:
            release = ctx.mb.release(rel_id)
        except Exception:
            release = None
    if release is None:
        release = choose_release(rec, info.album, ctx.confirmed_ids)

    print(f"  Override: {artist_credit_name(rec)} - {clean_text(rec.get('title'))}")
    entry = {"source": "musicbrainz", "recording_id": rec_id,
             "release_id": release["id"] if release else None, "via": "override"}
    return build_match_from_musicbrainz(rec, release, ctx.mb, via="override"), entry, False


# ----------------------------- orchestration ---------------------------------


def read_file_info(path: Path) -> Optional[FileInfo]:
    try:
        mp4 = MP4(str(path))
    except Exception as exc:
        print(f"[SKIP] {path.name}: cannot read as MP4/M4A ({exc})")
        return None

    title = get_first(mp4.tags, "\xa9nam") or path.stem
    # Several contributing artists are stored as separate values; rejoin them so
    # searches and cache keys see the full credit ("A & B"), not just the first.
    artist = " & ".join(get_all(mp4.tags, "\xa9ART")) or get_first(mp4.tags, "aART")
    artist = artist.replace(ARTIST_SEPARATOR, " & ")  # keep searches and cache keys stable

    # With no artist tag at all, recover one from an "Artist - Title" string
    # rather than skipping the file outright as the old version did.
    if not artist:
        guessed_artist, guessed_title = split_artist_title(title)
        if guessed_artist:
            artist, title = guessed_artist, guessed_title

    return FileInfo(
        path=path, title=title, artist=artist,
        album=get_first(mp4.tags, "\xa9alb"), dur=duration_seconds(mp4),
    )


def match_from_cache(entry: dict, ctx: Context) -> Optional[TagMatch]:
    source = entry.get("source", "musicbrainz")
    via = entry.get("via", "cache")
    try:
        if source == "theaudiodb":
            if entry.get("track_id") and ctx.adb is not None:
                track = ctx.adb.track_by_id(entry["track_id"])
                return build_match_from_audiodb(track, ctx.adb) if track else None
            return None
        if source == "musicbrainz":
            release = ctx.mb.release(entry["release_id"]) if entry.get("release_id") else None
            if entry.get("recording_id"):
                rec = ctx.mb.recording(entry["recording_id"])
                return build_match_from_musicbrainz(rec, release, ctx.mb, via=via)
            if release is not None:
                found = match_title_in_tracklist(entry.get("title", ""), tracklist(release), 0.45)
                if found:
                    return build_match_from_tracklist_entry(found[0], release, ctx.mb, via=via)
            return None
    except Exception:
        return None
    return None


def remember_release(match: TagMatch, ctx: Context):
    """Feed a confirmed release back into the pool for later files."""
    if match.source != "musicbrainz" or not match.mb_release_id:
        return
    try:
        release = ctx.mb.release(match.mb_release_id)
    except Exception:
        return
    if len(tracklist(release)) > 1:
        ctx.release_pool[match.mb_release_id] = release
        ctx.confirmed_ids.add(match.mb_release_id)


def resolve_match(info: FileInfo, ctx: Context, album_context: bool = True):
    """Returns (match, explicit_skip)."""
    cache_key = f"match:{info.key}"

    if not ctx.force:
        cached = ctx.mb.cache.get(cache_key)
        if cached:
            match = match_from_cache(cached, ctx)
            if match:
                return match, False
            print("  Cached match is no longer available; searching again.")

    stages = [("override", resolve_from_override), ("musicbrainz", resolve_from_musicbrainz)]
    if album_context:
        stages.append(("album-context", resolve_from_album_context))
    stages.append(("acoustid", resolve_from_acoustid))
    stages.append(("theaudiodb", resolve_from_audiodb))

    for name, resolver in stages:
        if name == "album-context" and (ctx.release_pool or info.album):
            print("  Trying album context...")
        elif name == "theaudiodb" and ctx.adb is not None:
            print("  Trying TheAudioDB fallback...")
        match, entry, skipped = resolver(info, ctx)
        if skipped:
            return None, True
        if match:
            if entry:
                ctx.mb.cache[cache_key] = entry
                ctx.mb.save_cache()
            return match, False

    return None, False


def finish_file(info: FileInfo, match: TagMatch, ctx: Context) -> bool:
    def splitter(value: str) -> list:
        return split_artists(value, ctx.mb)

    try:
        new_path, release_desc = apply_match_tags(info.path, match, info.album, ctx.rename, splitter)
    except Exception as exc:
        print(f"  Tagging failed: {exc}")
        return False
    remember_release(match, ctx)
    track = f"track {match.track_no}/{match.track_total}" if (match.is_multi and match.track_no) else "single (1/1)"
    print(f"  [OK] {new_path.name} — {release_desc}, {track} (via {match.source}/{match.via})")

    lyric_artist = match.artists[0] if match.artists else primary_artist(match.artist or info.artist)
    add_lyrics(new_path, match.title or info.title, lyric_artist, ctx)
    return True


def process(info: FileInfo, ctx: Context, album_context: bool = True) -> bool:
    print(f"\nTagging: {info.artist or '(unknown artist)'} - {info.title}")
    print(f"  Album hint: {info.album or '(none)'}")

    match, skipped = resolve_match(info, ctx, album_context=album_context)
    if skipped or not match:
        return False
    return finish_file(info, match, ctx)


def add_lyrics(path: Path, title: str, artist: str, ctx: Context) -> None:
    """Look up lyrics (Musixmatch, then Genius) and store them in the lyrics tag."""
    lc = ctx.lyrics
    if lc is None or not lc.enabled:
        return
    title = base_title(title) or title
    artist = primary_artist(artist)
    if not title or not artist:
        return

    try:
        mp4 = MP4(str(path))
    except Exception:
        return
    if mp4.tags is None:
        mp4.add_tags()
    if mp4.tags.get("\xa9lyr") and not ctx.relyrics:
        return

    miss_key = f"lyrics:miss:{norm(artist)}|{norm(title)}"
    if ctx.mb.cache.get(miss_key) == lc.signature and not (ctx.force or ctx.relyrics):
        return

    result = lc.fetch(artist, title)
    if not result:
        print("  Lyrics: not found.")
        ctx.mb.cache[miss_key] = lc.signature
        ctx.mb.save_cache()
        return

    mp4.tags["\xa9lyr"] = [result.text]
    mp4.save()
    note = " (30% preview)" if result.partial else ""
    print(f"  Lyrics: added from {result.source}, {len(result.text.splitlines())} lines{note}")


def finalize_unmatched(info: FileInfo, ctx: Context) -> None:
    """
    Best-effort cleanup for a file that couldn't be identified: split combined
    artists, crop the thumbnail to a square, and add lyrics. Title, album and
    track number are left exactly as they were.
    """
    try:
        mp4 = MP4(str(info.path))
    except Exception:
        return
    if mp4.tags is None:
        mp4.add_tags()

    changes = []
    current = get_all(mp4.tags, "\xa9ART")
    names: list = []
    for value in current:
        names.extend(split_artists(value, ctx.mb))
    names = list(dict.fromkeys(names))
    joined = ARTIST_SEPARATOR.join(names)
    if len(names) > 1 and current != [joined]:
        mp4.tags["\xa9ART"] = [joined]
        mp4.tags["aART"] = [names[0]]
        mp4.tags["----:com.apple.iTunes:ARTISTS"] = [n.encode() for n in names]
        changes.append("separate artists: " + ", ".join(names))

    if square_existing_cover(mp4):
        changes.append("cover cropped to 1:1")

    if changes:
        mp4.save()
        print(f"  {info.path.name}: " + "; ".join(changes))

    add_lyrics(info.path, info.title, names[0] if names else info.artist, ctx)


# ----------------------------- downloader -----------------------------------


def check_dependencies():
    if shutil.which("yt-dlp") is None:
        print("ERROR: yt-dlp was not found in PATH.")
        print("Install/update it with: py -m pip install -U yt-dlp")
        sys.exit(1)
    try:
        import mutagen  # noqa: F401
        import requests  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        print("ERROR: Python dependencies are missing.")
        print("Run: py -m pip install -r requirements.txt")
        sys.exit(1)


def _run_ytdlp(command: list, folder: Path) -> int:
    print("\n=== Downloading ===")
    print(" ".join(f'"{x}"' if " " in x else x for x in command))
    print()
    return subprocess.run(command, cwd=str(folder)).returncode


def download(url: str, folder: Path, cookies: Optional[Path] = None) -> list:
    before = media_files(folder)
    run_list = folder / RUN_LIST_FILE
    run_list.unlink(missing_ok=True)
    command = [
        "yt-dlp", "--no-abort-on-error", "--ignore-errors",
        "--add-metadata", "--embed-thumbnail",
        "-f", "140/bestaudio[ext=m4a]/bestaudio",
        "-o", str(folder / "%(title)s.%(ext)s"),
    ]

    # Your own exported session cookies, so private/liked playlists resolve.
    if cookies is not None:
        if cookies.is_file():
            command += ["--cookies", str(cookies)]
            print(f"Using cookies: {cookies}")
        else:
            print(f"NOTE: no cookies file at {cookies}; continuing without it.")

    # Ask yt-dlp which files it actually produced or re-processed. Comparing the
    # folder before and after only ever finds brand-new files, so a song that
    # was already in the folder used to be skipped and never tagged again.
    code = _run_ytdlp(command + ["--print-to-file", "after_move:filepath", RUN_LIST_FILE, url], folder)
    if code == 2 and not run_list.exists():
        print("This yt-dlp doesn't accept --print-to-file; retrying without it.")
        print("Update it (py -m pip install -U yt-dlp) so existing songs get re-tagged too.")
        code = _run_ytdlp(command + [url], folder)
    if code not in (0, 1):
        print(f"yt-dlp exited with code {code}")

    handled: set = set()
    if run_list.exists():
        for line in run_list.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            candidate = Path(line)
            if not candidate.is_absolute():
                candidate = folder / candidate
            if candidate.is_file() and candidate.suffix.lower() in MEDIA_SUFFIXES:
                handled.add(candidate.resolve())
        run_list.unlink(missing_ok=True)

    new_files = media_files(folder) - before
    files = sorted(handled | new_files)
    existing = len(handled - new_files)
    if not files:
        print("No media files were downloaded or updated.")
    else:
        extra = f" ({existing} already in the folder)" if existing else ""
        print(f"\nTagging {len(files)} file(s) from this run{extra}.")
    return files


def write_unmatched_report(folder: Path, unmatched: list) -> Path:
    report = folder / UNMATCHED_FILE.name
    lines = [
        "Files that could not be identified.",
        "",
        "To fix one permanently, look it up on musicbrainz.org and add an entry",
        f"to {folder / OVERRIDES_FILE.name}, using the filename as the key:",
        "",
        "  {",
        '    "Some Song.m4a": { "recording": "<recording MBID or URL>" },',
        '    "Another Song.m4a": { "release": "<release MBID or URL>" }',
        "  }",
        "",
        'A "release" entry pins the album and then finds the track inside it by',
        "title, which is usually the easier of the two to look up.",
        "",
    ]
    for info in unmatched:
        lines.append(f"{info.path.name}\t[artist: {info.artist}] [title: {info.title}] [album: {info.album}]")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Download and tag music (MusicBrainz + album context + AcoustID + TheAudioDB)."
    )
    parser.add_argument("url", help="YouTube/YouTube Music URL")
    parser.add_argument("--yes", action="store_true", help="Automatically accept lower-confidence matches")
    parser.add_argument("--pick", action="store_true", help="Show a numbered candidate list for weak matches")
    parser.add_argument("--force", action="store_true", help="Ignore the match cache and search again")
    parser.add_argument("--no-download", action="store_true", help="Only tag existing M4A files in the library")
    parser.add_argument("--rename", action="store_true", help="Rename tagged files to Artist - Title.m4a")
    parser.add_argument("--folder", default=str(LIBRARY), help="Music library folder")
    parser.add_argument("--config", default=None, help="Settings file (default: musicdl.ini next to the script)")
    parser.add_argument("--cookies", default=str(COOKIES_FILE or ""),
                        help=f"cookies.txt passed to yt-dlp (default: {COOKIES_FILE or 'none'})")
    parser.add_argument("--no-audiodb", action="store_true", help="Disable the TheAudioDB fallback")
    parser.add_argument("--audiodb-key", default=None, help="Your own TheAudioDB API key")
    parser.add_argument("--acoustid-key", default=None, help="AcoustID API key (enables fingerprint matching)")
    parser.add_argument("--no-album-context", action="store_true", help="Disable album/tracklist matching")
    parser.add_argument("--musixmatch-key", default=None, help="Musixmatch API key (lyrics, first choice)")
    parser.add_argument("--genius-token", default=None, help="Genius access token (lyrics fallback)")
    parser.add_argument("--no-lyrics", action="store_true", help="Don't fetch lyrics")
    parser.add_argument("--relyrics", action="store_true", help="Replace lyrics already stored in the files")
    parser.add_argument("--partial-lyrics", action="store_true",
                        help="Accept Musixmatch's ~30%% free-plan preview when nothing better is found")
    parser.add_argument("--clear-cache", action="store_true", help="Delete the cache file before running")
    args = parser.parse_args()

    folder = Path(args.folder)
    folder.mkdir(parents=True, exist_ok=True)
    check_dependencies()

    if CONFIG_PATH:
        print(f"Settings: {CONFIG_PATH}")
    else:
        print("Settings: no musicdl.ini found, using built-in defaults "
              "(copy musicdl.example.ini to musicdl.ini to customise).")
    if not CONTACT:
        print("NOTE: set 'contact' under [general] in musicdl.ini; MusicBrainz asks for a way to reach you.")

    if args.clear_cache:
        cache_path = folder / CACHE_FILE.name
        if cache_path.exists():
            cache_path.unlink()
            print(f"Cleared cache: {cache_path}")

    mb = MBClient(folder / CACHE_FILE.name)
    adb = None if args.no_audiodb else AudioDBClient(mb.cache, mb.save_cache, api_key=args.audiodb_key or ADB_API_KEY)
    acoustid_key = args.acoustid_key or ACOUSTID_API_KEY
    acoustid = AcoustIDClient(acoustid_key, mb.cache, mb.save_cache) if acoustid_key else None

    if acoustid and not acoustid.available:
        print("NOTE: an AcoustID key is set but `fpcalc` is not on PATH, so")
        print("      fingerprint matching is disabled. Install Chromaprint and")
        print("      make sure `fpcalc -version` works.\n")

    lyrics = None
    if not args.no_lyrics:
        lyrics = LyricsClient(
            args.musixmatch_key or MUSIXMATCH_API_KEY,
            args.genius_token or GENIUS_ACCESS_TOKEN,
            allow_partial=args.partial_lyrics,
        )
        if not lyrics.enabled:
            print("NOTE: no Musixmatch key or Genius token set (see musicdl.ini), so lyrics are skipped.\n")
            lyrics = None

    ctx = Context(
        mb=mb, adb=adb, acoustid=acoustid, auto_accept=args.yes, pick=args.pick,
        force=args.force, rename=args.rename,
        overrides=load_overrides(folder / OVERRIDES_FILE.name),
        lyrics=lyrics, relyrics=args.relyrics,
    )
    if ctx.overrides:
        print(f"Loaded {len(ctx.overrides)} manual override(s).")

    cookies = Path(args.cookies) if args.cookies else None
    files = sorted(folder.glob("*.m4a")) if args.no_download else download(args.url, folder, cookies)
    if not files:
        print("\nNothing to tag.")
        return 1

    infos = [i for i in (read_file_info(p) for p in files) if i is not None]
    album_context = not args.no_album_context

    print("\n=== Tagging (pass 1) ===")
    tagged = set()
    for info in infos:
        if process(info, ctx, album_context=album_context):
            tagged.add(info.path)

    # Pass 2: now that every release in this run is known, retry the leftovers
    # against those tracklists. This is what rescues the one song on an album
    # whose siblings all matched but which itself did not -- in pass 1 that
    # song may simply have been processed before its siblings.
    leftovers = [i for i in infos if i.path not in tagged]
    if leftovers and album_context and ctx.release_pool:
        print(f"\n=== Tagging (pass 2: album context for {len(leftovers)} leftover file(s)) ===")
        for info in leftovers:
            print(f"\nRetrying: {info.artist} - {info.title}")
            match, entry, skipped = resolve_from_album_context(info, ctx, search_album=False)
            if skipped or not match:
                continue
            if finish_file(info, match, ctx):
                if entry:
                    ctx.mb.cache[f"match:{info.key}"] = entry
                    ctx.mb.save_cache()
                tagged.add(info.path)

    unmatched = [i for i in infos if i.path not in tagged]

    if unmatched:
        print(f"\n=== Cleaning up {len(unmatched)} unidentified file(s) ===")
        for info in unmatched:
            finalize_unmatched(info, ctx)

    print("\n=== Finished ===")
    print(f"Successfully tagged: {len(tagged)}/{len(infos)}")
    print(f"Library: {folder}")
    print(f"Cache:   {folder / CACHE_FILE.name}")

    if unmatched:
        report = write_unmatched_report(folder, unmatched)
        print(f"\nUnidentified: {len(unmatched)} file(s). Details written to:")
        print(f"  {report}")
        for info in unmatched[:10]:
            print(f"  - {info.path.name}")
        if len(unmatched) > 10:
            print(f"  ... and {len(unmatched) - 10} more")

    if lyrics is not None and lyrics.saw_partial:
        print("\nNOTE: Musixmatch's free plan only returns a ~30% lyric preview, so some lyrics were skipped.")
        print("      Add a Genius token (--genius-token) or accept previews with --partial-lyrics.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
