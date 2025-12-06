#!/usr/bin/env bash
set -euo pipefail

PID=$$
SOCK="/tmp/mpv-${PID}.sock"
TMP_ROOT=""
BUFFER_AHEAD=1

AUDIO_EXTS=(flac mp3 ogg opus m4a alac wav aiff wv)
PLAYLIST_EXTS=(m3u m3u8 pls cue)
IMAGE_EXTS=(jpg jpeg png webp gif bmp tiff tif svg)
PREFERRED_IMAGE_KEYWORDS=(cover front folder)
NON_FRONT_IMAGE_KEYWORDS=(back tray cd disc inlay inlet booklet book spine rear inside tracklisting)
TINY_FRONT_AREA=200000           # pixels (~0.2MP) threshold for treating front-ish art as tiny
IMAGE_PROBE_BIN="ffprobe"
IMAGE_EXTRACT_BIN="ffmpeg"
COVER_PREFERRED_FILE="cover.png"
AREA_THRESHOLD_PCT=75            # Lower-scope image can beat higher-scope/keyword if within this % of area (or vice versa)
ALBUM_SPREAD_THRESHOLD=50        # Album-aware random kicks in when >= this many albums
ALBUM_HISTORY_MIN=20             # Minimum recent albums to avoid
ALBUM_HISTORY_MAX=200            # Maximum recent albums to avoid
ALBUM_HISTORY_PCT=10             # Target percent of albums to keep in history (avoid repeats)
ALBUM_HISTORY_SIZE=0             # Filled by planner for random mode
ALBUM_HISTORY=()
LAST_RANDOM_RESCAN=0
RANDOM_RESCAN_INTERVAL=3600 # seconds
ALBUM_SPREAD_MODE=0
TOTAL_ALBUM_COUNT=0
TOTAL_TRACK_COUNT=0
MODE=""
LIBRARY=""
ALBUM_DIR=""
PLAYLIST_FILE=""
RANDOM_MODE=""
NORMALIZE=0
METAFLAC_AVAILABLE=0
WARNED_NO_METAFLAC=0
DISPLAY_ROOT=""
ART_DEBUG=${ART_DEBUG:-0}

MPV_USER_ARGS=()
TRACKS=()
COVER_CHOICE_PATHS=()
COVER_CHOICE_META=()
COVER_CHOICE_DETAIL=()
COVER_SELECTED_BEST=""
COVER_SELECTED_META=""
COVER_SELECTED_DETAIL=""
ALBUMS=()
declare -A ALBUM_TRACK_FILES=()
declare -A ALBUM_TRACK_COUNT=()

log_info() { printf '[info] %s\n' "$*" >&2; }
log_warn() { printf '[warn] %s\n' "$*" >&2; }
log_error() { printf '[error] %s\n' "$*" >&2; }
die() {
  log_error "$*"
  exit 1
}

usage() {
  cat <<'USAGE'
Usage:
  mpv_music_wrapper.sh --random-mode=full-library --library /path/to/lib [--normalize] [mpv args...]
  mpv_music_wrapper.sh --album /path/to/album [--normalize] [mpv args...]
  mpv_music_wrapper.sh --playlist /path/to/list.m3u [--normalize] [mpv args...]

Modes (choose one):
  --random-mode=full-library   Shuffle any audio file under --library recursively.
  --album <dir>                Play audio files under <dir> (sorted, non-random).
  --playlist <file>            Play entries from <file> (sorted as given).

Options:
  --library <dir>              Required for --random-mode. Optional for --album to enable multi-disc
                               parent cover search when the album is inside the library.
  --normalize                  Copy to RAM, strip existing RG tags, add track RG via metaflac,
                               and play with --replaygain=track. Without this flag we still
                               copy to RAM, strip RG tags when possible, and link album art,
                               but do NOT compute RG or pass --replaygain to mpv.
  -h, --help                   Show this help.

Examples:
  mpv_music_wrapper.sh --random-mode=full-library --library /music --normalize
  mpv_music_wrapper.sh --album /music/Artist/Album
  mpv_music_wrapper.sh --playlist ~/lists/favorites.m3u8 --normalize
USAGE
}

lower_ext() {
  local f="$1"
  local base=${f##*.}
  printf '%s' "${base,,}"
}

display_path() {
  local p="$1"
  [[ -z "$p" ]] && return 0
  if [[ -n "$DISPLAY_ROOT" ]]; then
    case "$p" in
    "$DISPLAY_ROOT")
      printf '.'
      return
      ;;
    "$DISPLAY_ROOT"/*)
      printf '%s' "${p#${DISPLAY_ROOT}/}"
      return
      ;;
    esac
  fi
  printf '%s' "$p"
}

strip_ansi() {
  sed $'s/\x1B\\[[0-9;]*[mK]//g' <<<"$1"
}

visible_len() {
  local s
  s=$(strip_ansi "$1")
  # Approximate emoji width as 2 columns
  s=${s//🎵/aa}
  s=${s//🔀/aa}
  s=${s//💾/aa}
  s=${s//🔁/aa}
  s=${s//🎲/aa}
  s=${s//💿/aa}
  s=${s//🎯/aa}
  s=${s//📜/aa}
  echo "${#s}"
}

normalize_name_tokens() {
  local s="$1"
  s=${s,,}
  s=$(tr -cs '[:alnum:]' ' ' <<<"$s")
  read -r -a toks <<<"$s"
  printf '%s\n' "${toks[@]}"
}

clean_album_tokens() {
  local -a toks=()
  mapfile -t toks <<<"$(normalize_name_tokens "$1")"
  local -a cleaned=()
  local t
  for t in "${toks[@]}"; do
    [[ -z "$t" ]] && continue
    if [[ "$t" =~ ^[0-9]+$ ]]; then
      # Drop pure numbers
      continue
    fi
    if (( ${#t} <= 2 )); then
      continue
    fi
    local ext
    for ext in "${AUDIO_EXTS[@]}"; do
      if [[ "$t" == "$ext" ]]; then
        continue 2
      fi
    done
    cleaned+=("$t")
  done
  printf '%s\n' "${cleaned[@]}"
}

token_overlap_score() {
  local -n base_toks="$1"
  local -n target_toks="$2"
  local -A target_set=()
  local t
  for t in "${target_toks[@]}"; do
    target_set["$t"]=1
  done
  local score=0
  for t in "${base_toks[@]}"; do
    [[ -n "$t" && -n "${target_set[$t]:-}" ]] && ((score++))
  done
  echo "$score"
}

human_rescan_interval() {
  ((RANDOM_RESCAN_INTERVAL > 0)) || {
    echo "off"
    return
  }
  local _m _h
  _m=$((RANDOM_RESCAN_INTERVAL / 60))
  _h=$((_m / 60))
  _m=$((_m % 60))
  if ((_h > 0)); then
    printf "%dh%02dm" "$_h" "$_m"
  else
    printf "%dm" "$_m"
  fi
}

print_header() {
  local total="$1"

  local mode_line="" path_line=""
  local random_album_line="" random_desc_line=""
  local rescan_pretty
  rescan_pretty=$(human_rescan_interval)

  case "$MODE" in
  random)
    mode_line="🔀 Mode: random"
    path_line="💾 Library: $LIBRARY"
    random_album_line="💿 Albums: $TOTAL_ALBUM_COUNT"
    if ((ALBUM_SPREAD_MODE)); then
      random_desc_line=$(printf '🔁 I rotate albums, skip the last %d of %d, and look for new music every %s.' "$ALBUM_HISTORY_SIZE" "$TOTAL_ALBUM_COUNT" "$rescan_pretty")
    else
      random_desc_line=$(printf '🎲 Single big shuffle (library has < %d albums).' "$ALBUM_SPREAD_THRESHOLD")
    fi
    ;;
  album)
    mode_line="🎯 Mode: album"
    path_line="💾 Album: $ALBUM_DIR"
    ;;
  playlist)
    mode_line="📜 Mode: playlist"
    path_line="💾 Playlist: $PLAYLIST_FILE"
    ;;
  *)
    mode_line="Mode: unknown"
    path_line=""
    ;;
  esac

  local -a header_lines=()
  local title_line=$'\033[35m🎵 mpv music wrapper 🎵\033[0m'
  header_lines+=("$title_line")
  header_lines+=("---")
  if [[ "$MODE" == "random" ]]; then
    [[ -n "$path_line" ]] && header_lines+=("$path_line")
    header_lines+=("$mode_line")
    [[ -n "$random_desc_line" ]] && header_lines+=("$random_desc_line")
    [[ -n "$random_album_line" ]] && header_lines+=("$random_album_line")
  else
    [[ -n "$path_line" ]] && header_lines+=("$path_line")
    header_lines+=("$mode_line")
  fi
  header_lines+=("Tracks: $total")
  header_lines+=("---")
  header_lines+=("Socket: $SOCK")
  header_lines+=("Buffer ahead: $BUFFER_AHEAD")
  if ((NORMALIZE)); then
    header_lines+=("Normalize: enabled (ReplayGain track)")
  else
    header_lines+=("Normalize: disabled")
  fi

  local max_len=0 line
  for line in "${header_lines[@]}"; do
    [[ "$line" == "---" ]] && continue
    local vlen
    vlen=$(visible_len "$line")
    ((vlen > max_len)) && max_len=$vlen
  done
  local inner_width=$max_len

  printf '\033[36m╔' >&2
  printf '═%.0s' $(seq 1 $((inner_width + 2))) >&2
  printf '╗\033[0m\n' >&2

  local is_first=1
  for line in "${header_lines[@]}"; do
    if [[ "$line" == "---" ]]; then
      printf '\033[36m╟' >&2
      printf '─%.0s' $(seq 1 $((inner_width + 2))) >&2
      printf '╢\033[0m\n' >&2
      continue
    fi
    local pad_len vlen
    vlen=$(visible_len "$line")
    pad_len=$((inner_width - vlen))
    ((pad_len < 0)) && pad_len=0
    local left_pad=0
    if ((is_first)); then
      left_pad=$((pad_len / 2))
      pad_len=$((pad_len - left_pad))
      is_first=0
    fi
    printf '\033[36m║\033[0m ' >&2
    printf '%*s' "$left_pad" "" >&2
    printf '%s' "$line" >&2
    printf '%*s' "$pad_len" "" >&2
    printf ' \033[36m║\033[0m\n' >&2
  done

  printf '\033[36m╚' >&2
  printf '═%.0s' $(seq 1 $((inner_width + 2))) >&2
  printf '╝\033[0m\n' >&2
}

ext_in_list() {
  local ext="$1"
  shift
  local item
  for item in "$@"; do
    if [[ "$ext" == "$item" ]]; then
      return 0
    fi
  done
  return 1
}

is_audio() {
  local ext
  ext=$(lower_ext "$1")
  ext_in_list "$ext" "${AUDIO_EXTS[@]}"
}

is_image() {
  local ext
  ext=$(lower_ext "$1")
  ext_in_list "$ext" "${IMAGE_EXTS[@]}"
}

find_images_recursive() {
  local dir="$1"
  find "$dir" -type f \( \
    -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o \
    -iname '*.gif' -o -iname '*.bmp' -o -iname '*.tif' -o -iname '*.tiff' -o -iname '*.svg' \
    \) -print0 2>/dev/null
}

album_root_for_track() {
  # For multi-disc albums stored as <library>/<album>/<disc>/<tracks>, return
  # the album folder directly under the library root. Only applies when a
  # library is known and the track resides inside it.
  local track="$1" lib="${LIBRARY%/}"
  [[ -n "$lib" && -d "$lib" ]] || return 0

  case "$track" in
  "$lib"/*) ;;
  *) return 0 ;;
  esac

  local rel="${track#$lib/}"
  local first="${rel%%/*}"
  [[ -z "$first" ]] && return 0

  local root="$lib/$first"
  [[ -d "$root" ]] || return 0
  printf '%s\n' "$root"
}

parse_args() {
  if [[ $# -eq 0 ]]; then
    usage
    exit 1
  fi

  local arg
  for arg in "$@"; do
    case "$arg" in
    --random-mode=full-library)
      MODE="random"
      RANDOM_MODE="full-library"
      ;;
    --library=*)
      LIBRARY="${arg#*=}"
      ;;
    --album=*)
      MODE="album"
      ALBUM_DIR="${arg#*=}"
      ;;
    --playlist=*)
      MODE="playlist"
      PLAYLIST_FILE="${arg#*=}"
      ;;
    --normalize)
      NORMALIZE=1
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      MPV_USER_ARGS+=("$arg")
      ;;
    esac
  done

  case "$MODE" in
  random)
    [[ "$RANDOM_MODE" == "full-library" ]] || die "Unsupported random mode: ${RANDOM_MODE:-<none>}"
    [[ -n "$LIBRARY" ]] || die "--library is required for --random-mode=full-library"
    [[ -d "$LIBRARY" ]] || die "Library path not found: $LIBRARY"
    ;;
  album)
    [[ -n "$ALBUM_DIR" ]] || die "--album requires a directory path"
    [[ -d "$ALBUM_DIR" ]] || die "Album directory not found: $ALBUM_DIR"
    if [[ -n "$LIBRARY" ]]; then
      [[ -d "$LIBRARY" ]] || die "Library path not found: $LIBRARY"
    fi
    ;;
  playlist)
    [[ -n "$PLAYLIST_FILE" ]] || die "--playlist requires a file path"
    [[ -f "$PLAYLIST_FILE" ]] || die "Playlist file not found: $PLAYLIST_FILE"
    ;;
  *)
    die "One mode is required: --random-mode=full-library, --album, or --playlist"
    ;;
  esac
}

set_display_root() {
  case "$MODE" in
  random) DISPLAY_ROOT="${LIBRARY%/}" ;;
  album) DISPLAY_ROOT="${ALBUM_DIR%/}" ;;
  playlist) DISPLAY_ROOT=$(cd "$(dirname "$PLAYLIST_FILE")" && pwd) ;;
  esac
  if [[ -z "$DISPLAY_ROOT" ]]; then
    DISPLAY_ROOT="/"
  fi
}

ensure_tmp_root() {
  if [[ -n "$TMP_ROOT" && -d "$TMP_ROOT" ]]; then
    return
  fi
  TMP_ROOT=$(mktemp -d "/dev/shm/mpv-music-${PID}-XXXXXX" 2>/dev/null) || die "Could not create temporary directory under /dev/shm (permission denied?)"
}

check_dependencies() {
  command -v mpv >/dev/null 2>&1 || die "mpv not found in PATH"
  command -v python >/dev/null 2>&1 || die "python not found in PATH"
  command -v "$IMAGE_PROBE_BIN" >/dev/null 2>&1 || die "ffprobe not found in PATH"
  command -v "$IMAGE_EXTRACT_BIN" >/dev/null 2>&1 || die "ffmpeg not found in PATH"
  if command -v metaflac >/dev/null 2>&1; then
    METAFLAC_AVAILABLE=1
  else
    METAFLAC_AVAILABLE=0
    if ((NORMALIZE)); then
      die "--normalize requested but metaflac not found"
    fi
  fi
}

maybe_warn_metaflac() {
  if ((METAFLAC_AVAILABLE == 0 && WARNED_NO_METAFLAC == 0)); then
    WARNED_NO_METAFLAC=1
    log_warn "metaflac not found; FLAC RG tag stripping/scan skipped."
  fi
}

add_track() {
  TRACKS+=("$1")
}

gather_image_candidates() {
  local dir="$1" album_root="$2" is_multi="$3" audio_copy="$4" extract_dir="$5"
  local out_name="$6"
  local embedded_name="$7"
  local -n out_arr="$out_name"
  local -n embedded_ref="$embedded_name"

  local -A seen=()
  out_arr=()

  local -a tmp_candidates=()
  mapfile -d '' -t tmp_candidates < <(find_images_recursive "$dir" || true)
  local f
  for f in "${tmp_candidates[@]}"; do
    [[ -n "${seen[$f]:-}" ]] && continue
    seen["$f"]=1
    out_arr+=("$f")
  done

  if ((is_multi)); then
    mapfile -d '' -t tmp_candidates < <(find_images_recursive "$album_root" || true)
    for f in "${tmp_candidates[@]}"; do
      [[ -n "${seen[$f]:-}" ]] && continue
      seen["$f"]=1
      out_arr+=("$f")
    done
  fi

  embedded_ref=$(extract_embedded_cover "$audio_copy" "$extract_dir" 2>/dev/null || true)
  if [[ -n "$embedded_ref" ]]; then
    out_arr+=("$embedded_ref")
  fi
}

gather_random_tracks() {
  build_album_map
  if ((ALBUM_SPREAD_MODE)); then
    ((${#ALBUMS[@]} > 0)) || die "No albums with audio files found under $LIBRARY"
    return
  fi

  TRACKS=()
  local album
  local -a tmp_tracks=()
  for album in "${ALBUMS[@]}"; do
    mapfile -t tmp_tracks <<<"${ALBUM_TRACK_FILES[$album]}"
    TRACKS+=("${tmp_tracks[@]}")
  done

  mapfile -d '' -t TRACKS < <(printf '%s\0' "${TRACKS[@]}" | shuf -z)
  ((${#TRACKS[@]} > 0)) || die "No audio files found under $LIBRARY"
}

build_album_map() {
  ALBUMS=()
  ALBUM_TRACK_FILES=()
  ALBUM_TRACK_COUNT=()
  TOTAL_TRACK_COUNT=0

  mapfile -d '' -t local_albums < <(find "$LIBRARY" -mindepth 1 -maxdepth 1 -type d -print0)
  local album
  for album in "${local_albums[@]}"; do
    mapfile -d '' -t tracks < <(
      find "$album" -type f -print0 |
        while IFS= read -r -d '' f; do
          if is_audio "$f"; then
            printf '%s\0' "$f"
          fi
        done
    )
    ((${#tracks[@]})) || continue
    ALBUMS+=("$album")
    ALBUM_TRACK_COUNT["$album"]=${#tracks[@]}
    ALBUM_TRACK_FILES["$album"]=$(printf '%s\n' "${tracks[@]}")
    TOTAL_TRACK_COUNT=$((TOTAL_TRACK_COUNT + ${#tracks[@]}))
  done

  TOTAL_ALBUM_COUNT=${#ALBUMS[@]}
  ALBUM_SPREAD_MODE=0
  if ((TOTAL_ALBUM_COUNT >= ALBUM_SPREAD_THRESHOLD)); then
    ALBUM_SPREAD_MODE=1
    ALBUM_HISTORY_SIZE=$((TOTAL_ALBUM_COUNT * ALBUM_HISTORY_PCT / 100))
    ((ALBUM_HISTORY_SIZE < ALBUM_HISTORY_MIN)) && ALBUM_HISTORY_SIZE=$ALBUM_HISTORY_MIN
    ((ALBUM_HISTORY_SIZE > ALBUM_HISTORY_MAX)) && ALBUM_HISTORY_SIZE=$ALBUM_HISTORY_MAX
    ((ALBUM_HISTORY_SIZE >= TOTAL_ALBUM_COUNT)) && ALBUM_HISTORY_SIZE=$((TOTAL_ALBUM_COUNT - 1))
  fi
  LAST_RANDOM_RESCAN=$(date +%s)
}

prune_album_history() {
  # Drop history entries that no longer exist after a rescan
  local -a pruned=()
  local h
  for h in "${ALBUM_HISTORY[@]}"; do
    [[ -n "${ALBUM_TRACK_COUNT[$h]+x}" ]] && pruned+=("$h")
  done
  ALBUM_HISTORY=("${pruned[@]}")
}

album_history_contains() {
  local album="$1" limit="$2"
  local count=${#ALBUM_HISTORY[@]}
  ((count == 0)) && return 1
  local start=0
  if ((count > limit)); then
    start=$((count - limit))
  fi
  local i
  for ((i = count - 1; i >= start; i--)); do
    if [[ "${ALBUM_HISTORY[$i]}" == "$album" ]]; then
      return 0
    fi
  done
  return 1
}

push_album_history() {
  local album="$1"
  ALBUM_HISTORY+=("$album")
  local max_size=$ALBUM_HISTORY_SIZE
  ((max_size > TOTAL_ALBUM_COUNT - 1)) && max_size=$((TOTAL_ALBUM_COUNT - 1))
  while ((${#ALBUM_HISTORY[@]} > max_size)); do
    ALBUM_HISTORY=("${ALBUM_HISTORY[@]:1}")
  done
}

pick_random_from_array() {
  local -n arr="$1"
  ((${#arr[@]} > 0)) || return 1
  local choice
  choice=$(printf '%s\0' "${arr[@]}" | shuf -z -n1 | tr -d '\0')
  [[ -n "$choice" ]] || return 1
  printf '%s\n' "$choice"
}

choose_album_for_play() {
  local album_count=${#ALBUMS[@]}
  ((album_count > 0)) || return 1
  local hist_cap=$ALBUM_HISTORY_SIZE
  ((hist_cap > album_count - 1)) && hist_cap=$((album_count - 1))

  local -a candidates=()
  declare -A blocked=()
  local h
  for h in "${ALBUM_HISTORY[@]}"; do
    blocked["$h"]=1
  done

  local a
  for a in "${ALBUMS[@]}"; do
    [[ -n "${blocked[$a]:-}" ]] && continue
    candidates+=("$a")
  done
  if ((${#candidates[@]} == 0)); then
    candidates=("${ALBUMS[@]}")
  fi
  pick_random_from_array candidates
}

choose_track_in_album() {
  local album="$1"
  local tracks_str="${ALBUM_TRACK_FILES[$album]-}"
  [[ -n "$tracks_str" ]] || return 1
  mapfile -t local_tracks <<<"$tracks_str"
  pick_random_from_array local_tracks
}

maybe_refresh_album_map() {
  local now
  now=$(date +%s)
  if ((now - LAST_RANDOM_RESCAN < RANDOM_RESCAN_INTERVAL)); then
    return
  fi

  local old_track_count=$TOTAL_TRACK_COUNT
  local -a old_albums=("${ALBUMS[@]}")
  declare -A old_set=()
  local a
  for a in "${old_albums[@]}"; do
    old_set["$a"]=1
  done

  build_album_map
  prune_album_history

  local added=0 removed=0
  for a in "${ALBUMS[@]}"; do
    [[ -n "${old_set[$a]:-}" ]] || ((added++))
  done
  for a in "${old_albums[@]}"; do
    [[ -n "${ALBUM_TRACK_COUNT[$a]+x}" ]] || ((removed++))
  done

  local delta=$((TOTAL_TRACK_COUNT - old_track_count))
  if ((added > 0 || removed > 0 || delta != 0)); then
    log_info "random rescan: albums=$TOTAL_ALBUM_COUNT (added ${added}, removed ${removed}) tracks=$TOTAL_TRACK_COUNT (delta $delta)"
  fi
}

next_album_spread_track() {
  maybe_refresh_album_map
  ((ALBUM_SPREAD_MODE)) || return 1
  ((${#ALBUMS[@]} > 0)) || return 1

  local album
  album=$(choose_album_for_play) || return 1
  local track
  track=$(choose_track_in_album "$album") || return 1

  push_album_history "$album"
  printf '%s\n' "$track"
}

gather_album_tracks() {
  mapfile -d '' -t TRACKS < <(
    find "$ALBUM_DIR" -type f -print0 |
      sort -z |
      while IFS= read -r -d '' f; do
        if is_audio "$f"; then
          printf '%s\0' "$f"
        fi
      done
  )
  ((${#TRACKS[@]} > 0)) || die "No audio files found in album directory"
}

parse_m3u_like() {
  local file="$1" dir="$2"
  local line path
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
    if [[ "$line" = /* || "$line" =~ ^[A-Za-z]:[\\/].* ]]; then
      path="$line"
    else
      path="$dir/$line"
    fi
    if [[ -f "$path" ]] && is_audio "$path"; then
      add_track "$path"
    elif [[ -f "$path" ]]; then
      log_warn "Skipping non-audio entry in playlist: $path"
    fi
  done <"$file"
}

parse_pls() {
  local file="$1" dir="$2"
  local line key val path
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    if [[ "$line" =~ ^File[0-9]+= ]]; then
      key=${line%%=*}
      val=${line#*=}
      if [[ "$val" = /* || "$val" =~ ^[A-Za-z]:[\\/].* ]]; then
        path="$val"
      else
        path="$dir/$val"
      fi
      if [[ -f "$path" ]] && is_audio "$path"; then
        add_track "$path"
      elif [[ -f "$path" ]]; then
        log_warn "Skipping non-audio entry in playlist: $path"
      fi
    fi
  done <"$file"
}

parse_cue_minimal() {
  local file="$1" dir="$2"
  local line path
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    if [[ "$line" =~ ^[Ff][Ii][Ll][Ee][[:space:]]+\"([^\"]+)\" ]]; then
      path="${BASH_REMATCH[1]}"
      if [[ "$path" = /* || "$path" =~ ^[A-Za-z]:[\\/].* ]]; then
        :
      else
        path="$dir/$path"
      fi
      if [[ -f "$path" ]] && is_audio "$path"; then
        add_track "$path"
      elif [[ -f "$path" ]]; then
        log_warn "Skipping non-audio entry in playlist: $path"
      fi
    fi
  done <"$file"
}

gather_playlist_tracks() {
  local file="$PLAYLIST_FILE"
  local dir
  dir=$(cd "$(dirname "$file")" && pwd)

  local ext
  ext=$(lower_ext "$file")

  case "$ext" in
  m3u | m3u8)
    parse_m3u_like "$file" "$dir"
    ;;
  pls)
    parse_pls "$file" "$dir"
    ;;
  cue)
    parse_cue_minimal "$file" "$dir"
    ;;
  *)
    die "Unsupported playlist extension: $ext"
    ;;
  esac

  ((${#TRACKS[@]} > 0)) || die "No playable audio entries found in playlist"
}

image_dims_area() {
  local file="$1" info w h
  info=$("$IMAGE_PROBE_BIN" -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$file" 2>/dev/null | head -n1 || true)
  [[ -z "$info" ]] && {
    echo "0 0 0"
    return
  }
  w=${info%x*}
  h=${info#*x}
  [[ "$w" =~ ^[0-9]+$ && "$h" =~ ^[0-9]+$ ]] || {
    echo "0 0 0"
    return
  }
  echo "$w $h $((w * h))"
}

extract_embedded_cover() {
  local track="$1" dst_dir="$2"
  local out="$dst_dir/embedded-cover.png"
  mkdir -p "$dst_dir"
  "$IMAGE_EXTRACT_BIN" -loglevel error -y -i "$track" -map 0:v:0 -frames:v 1 "$out" 2>/dev/null || {
    rm -f -- "$out"
    return 1
  }
  [[ -s "$out" ]] || {
    rm -f -- "$out"
    return 1
  }
  echo "$out"
}

select_cover_for_track() {
  local track="$1" dst_dir="$2" audio_copy="$3"
  local -a candidates=()
  local embedded=""
  local dir f area kw name lower kwd dims w h size src_type disp_path kw_rank
  local best="" best_area=-1 best_kw_count=0 best_kw_rank=999 best_name="" best_src="external" best_w=0 best_h=0 best_size=-1 best_scope_rank=999
  local best_pref_kw_count=0 best_name_token_score=0 best_bucket=3 best_has_non_front=0
  local -a detail_lines=()

  COVER_SELECTED_META=""
  COVER_SELECTED_DETAIL="[ ] no images found"
  COVER_SELECTED_BEST=""

  dir=$(dirname "$track")
  local album_root=""
  local is_multi_disc=0
  album_root=$(album_root_for_track "$track" 2>/dev/null || true)
  if [[ -n "$album_root" && "$dir" != "$album_root" && "$dir" == "$album_root"* ]]; then
    is_multi_disc=1
  fi

  local album_token="${album_root##*/}"
  local -a album_tokens=()
  mapfile -t album_tokens <<<"$(clean_album_tokens "$album_token")"
  local base_root=""
  if [[ -n "$album_root" ]]; then
    base_root="${album_root%/}"
  else
    base_root="${dir%/}"
  fi

  gather_image_candidates "$dir" "$album_root" "$is_multi_disc" "$audio_copy" "$dst_dir" candidates embedded

  for f in "${candidates[@]}"; do
    dims=$(image_dims_area "$f")
    read -r w h area <<<"$dims"
    size=$(stat -c %s "$f" 2>/dev/null || echo 0)
    kw=0
    kw_rank=999
    local kw_count=0
    local pref_kw_count=0
    local name_token_score=0
    local bucket=3
    local has_non_front=0
    lower=$(basename "$f")
    lower=${lower,,}
    local base_noext="${lower%.*}"
    local -a base_tokens=()
    mapfile -t base_tokens <<<"$(normalize_name_tokens "$base_noext")"
    local idx=0
    for kwd in "${PREFERRED_IMAGE_KEYWORDS[@]}"; do
      if [[ "$lower" == *"$kwd"* ]]; then
        kw=1
        pref_kw_count=$((pref_kw_count + 1))
        kw_count=$((kw_count + 1))
        if ((kw_rank == 999)); then
          kw_rank=$idx
        fi
      fi
      idx=$((idx + 1))
    done
    local nfkw
    for nfkw in "${NON_FRONT_IMAGE_KEYWORDS[@]}"; do
      local tok
      for tok in "${base_tokens[@]}"; do
        if [[ "$tok" == "$nfkw" ]]; then
          has_non_front=1
          break 2
        fi
      done
    done

    if ((pref_kw_count == 0 && ${#album_tokens[@]} > 0)); then
      name_token_score=$(token_overlap_score base_tokens album_tokens)
      if ((name_token_score > 0)); then
        kw_count=$((kw_count + name_token_score))
      fi
    fi
    if ((pref_kw_count > 0 || (name_token_score > 0 && has_non_front == 0))); then
      bucket=1
    elif ((name_token_score > 0)); then
      bucket=2
    else
      bucket=3
    fi
    name=$(basename "$f")
    local scope="external" scope_rank=2
    if [[ -n "$embedded" && "$f" == "$embedded" ]]; then
      src_type="embedded"
      disp_path="(embedded from $(display_path "$track"))"
      scope="embedded"
      scope_rank=0
    else
      src_type="external"
      disp_path=$(display_path "$f")
      if [[ "$f" == "$dir"* ]]; then
        scope="disc"
        scope_rank=0
      elif [[ -n "$album_root" && "$f" == "$album_root"* ]]; then
        scope="album-root"
        scope_rank=1
      fi
    fi
    local rel_path="$disp_path"
    if [[ "$src_type" == "external" && -n "$base_root" ]]; then
      local prefix="${f:0:${#base_root}+1}"
      if [[ "$prefix" == "$base_root/" ]]; then
        rel_path="${f:${#base_root}+1}"
      fi
      rel_path="../$rel_path"
    elif [[ "$src_type" == "embedded" ]]; then
      rel_path="EMBEDDED"
    fi
    local area_mp
    area_mp=$(awk -v a="$area" 'BEGIN{printf "%.1fMP", a/1000000}')
    local size_mb
    size_mb=$(awk -v s="$size" 'BEGIN{printf "%.1fMB", s/1000000}')
    detail_lines+=("path=$rel_path res=${w}x${h} area=$area_mp size=$size_mb kwpref=$pref_kw_count nametoks=$name_token_score bucket=$bucket score=$kw_count")

    local pick=0
    local allow_worse_scope_override=0
    if ((scope_rank > best_scope_rank)); then
      if ((best_area > 0)); then
        if ((best_area * 100 < area * AREA_THRESHOLD_PCT)); then
          allow_worse_scope_override=1
        fi
      else
        allow_worse_scope_override=1
      fi
    fi

    if ((bucket < best_bucket)); then
      pick=1
    elif ((bucket == best_bucket && bucket == 1)); then
      if ((pref_kw_count > best_pref_kw_count)); then
        pick=1
      elif ((pref_kw_count == best_pref_kw_count)); then
        if ((pref_kw_count == 0 && has_non_front == 0 && name_token_score > best_name_token_score && area * 100 >= best_area * AREA_THRESHOLD_PCT)); then
          pick=1
        elif ((pref_kw_count == 0 && has_non_front == 0 && name_token_score == best_name_token_score && area * 100 >= best_area * AREA_THRESHOLD_PCT && scope_rank <= best_scope_rank)); then
          pick=1
        elif ((pref_kw_count > 0 && best_pref_kw_count > 0)); then
          if ((has_non_front == 0 && best_has_non_front == 1)); then
            # Prefer front-ish keyworded unless it is tiny
            if ((area >= TINY_FRONT_AREA)); then
              pick=1
            fi
          elif ((has_non_front == 1 && best_has_non_front == 0)); then
            # Only let non-front win if front-ish is tiny and non-front is much larger
            if ((best_area > 0 && best_area < TINY_FRONT_AREA)); then
              if ((area * 100 >= best_area * (100 + (100 - AREA_THRESHOLD_PCT)))); then
                pick=1
              fi
            fi
          elif ((scope_rank < best_scope_rank)); then
            pick=1
          elif ((scope_rank == best_scope_rank && area > best_area)); then
            pick=1
          elif ((scope_rank == best_scope_rank && area == best_area && kw_rank < best_kw_rank)); then
            pick=1
          elif ((scope_rank == best_scope_rank && area == best_area && kw_rank == best_kw_rank && size > best_size)); then
            pick=1
          elif ((scope_rank == best_scope_rank && area == best_area && kw_rank == best_kw_rank && size == best_size)) && { [[ -z "$best_name" ]] || [[ "$name" < "$best_name" ]]; }; then
            pick=1
          elif ((scope_rank == best_scope_rank && area == best_area && kw_rank == best_kw_rank && size == best_size)); then
            # Final tie among keyworded images: prefer current disc folder over sibling disc
            if [[ "$scope" == "disc" && "$dir" != "$album_root" && "$f" == "$dir"* && "$best" != "$dir"* ]]; then
              pick=1
            fi
          elif ((scope_rank > best_scope_rank && allow_worse_scope_override && area > best_area)); then
            pick=1
          fi
        elif ((scope_rank < best_scope_rank)); then
          pick=1
        elif ((scope_rank == best_scope_rank && area > best_area)); then
          pick=1
        elif ((scope_rank == best_scope_rank && area == best_area && kw_rank < best_kw_rank)); then
          pick=1
        elif ((scope_rank == best_scope_rank && area == best_area && kw_rank == best_kw_rank && size > best_size)); then
          pick=1
        elif ((scope_rank == best_scope_rank && area == best_area && kw_rank == best_kw_rank && size == best_size)) && { [[ -z "$best_name" ]] || [[ "$name" < "$best_name" ]]; }; then
          pick=1
        elif ((scope_rank > best_scope_rank && allow_worse_scope_override && area > best_area)); then
          pick=1
        fi
      elif ((pref_kw_count == 0 && has_non_front == 0 && best_pref_kw_count > 0 && name_token_score > 0 && area * 100 >= best_area * AREA_THRESHOLD_PCT)); then
        # No keywords but strong album-name and much larger than keyworded best
        pick=1
      fi
    elif ((bucket == best_bucket && bucket == 2)); then
      if ((name_token_score > best_name_token_score)); then
        pick=1
      elif ((name_token_score == best_name_token_score)); then
        local within_threshold=0
        if ((best_area > 0)); then
          if ((area * 100 >= best_area * AREA_THRESHOLD_PCT)); then
            within_threshold=1
          fi
        else
          within_threshold=1
        fi

        if ((scope_rank < best_scope_rank && within_threshold)); then
          pick=1
        elif ((scope_rank == best_scope_rank && area > best_area)); then
          pick=1
        elif ((scope_rank == best_scope_rank && area == best_area && size > best_size)); then
          pick=1
        elif ((scope_rank == best_scope_rank && area == best_area && size == best_size)) && { [[ -z "$best_name" ]] || [[ "$name" < "$best_name" ]]; }; then
          pick=1
        elif ((scope_rank > best_scope_rank && allow_worse_scope_override && area > best_area)); then
          pick=1
        fi
      fi
    elif ((bucket == best_bucket && bucket == 3)); then
      if ((scope_rank < best_scope_rank)); then
        pick=1
      elif ((scope_rank == best_scope_rank && area > best_area)); then
        pick=1
      elif ((scope_rank == best_scope_rank && area == best_area && kw_rank < best_kw_rank)); then
        pick=1
      elif ((scope_rank == best_scope_rank && area == best_area && kw_rank == best_kw_rank && size > best_size)); then
        pick=1
      elif ((scope_rank == best_scope_rank && area == best_area && kw_rank == best_kw_rank && size == best_size)) && { [[ -z "$best_name" ]] || [[ "$name" < "$best_name" ]]; }; then
        pick=1
      elif ((scope_rank > best_scope_rank && allow_worse_scope_override && area > best_area)); then
        pick=1
      fi
    elif ((kw_count == 0 && best_kw_count == 0)); then
      local within_threshold=0
      if ((best_area > 0)); then
        if ((area * 100 >= best_area * AREA_THRESHOLD_PCT)); then
          within_threshold=1
        fi
      else
        within_threshold=1
      fi

      if ((scope_rank < best_scope_rank && within_threshold)); then
        pick=1
      elif ((scope_rank == best_scope_rank && area > best_area)); then
        pick=1
      elif ((scope_rank == best_scope_rank && area == best_area && size > best_size)); then
        pick=1
      elif ((scope_rank == best_scope_rank && area == best_area && size == best_size)) && { [[ -z "$best_name" ]] || [[ "$name" < "$best_name" ]]; }; then
        pick=1
      elif ((scope_rank > best_scope_rank && allow_worse_scope_override && area > best_area)); then
        pick=1
      fi
    fi

    if ((pick)); then
      best="$f"
      best_area=$area
      best_kw_count=$kw_count
      best_kw_rank=$kw_rank
      best_pref_kw_count=$pref_kw_count
      best_name_token_score=$name_token_score
      best_name="$name"
      best_has_non_front=$has_non_front
      best_w=$w
      best_h=$h
      best_size=$size
      best_src=$src_type
      best_scope_rank=$scope_rank
      best_bucket=$bucket
    fi
  done

  if [[ -n "$embedded" && -n "$best" && "$embedded" != "$best" ]]; then
    rm -f -- "$embedded"
  fi

  if [[ -n "$best" ]]; then
    COVER_SELECTED_META="${best_src}|${best_w}|${best_h}|${best_area}|${best_kw_count}|${best_size}"
    local best_rel=""
    if [[ "$best_src" == "external" && -n "$base_root" ]]; then
      local best_prefix="${best:0:${#base_root}+1}"
      if [[ "$best_prefix" == "$base_root/" ]]; then
        best_rel="../${best:${#base_root}+1}"
      else
        best_rel="../$(display_path "$best")"
      fi
    fi
    if [[ "$best_src" == "embedded" ]]; then
      best_rel="EMBEDDED"
    fi
    [[ -z "$best_rel" && -n "$best" ]] && best_rel="$(display_path "$best")"
    local formatted=()
    for line in "${detail_lines[@]}"; do
      if [[ -n "$best_rel" && "$line" == path="$best_rel"* ]]; then
        formatted+=("[*] $line")
      elif [[ "$best_src" == "embedded" && "$line" == *"(embedded from $(display_path "$track"))"* ]]; then
        formatted+=("[*] $line")
      else
        formatted+=("[ ] $line")
      fi
    done
    COVER_SELECTED_DETAIL=$(printf '%s\n' "${formatted[@]}")
    COVER_SELECTED_BEST="$best"
  else
    COVER_SELECTED_DETAIL="[ ] no images found"
    COVER_SELECTED_BEST=""
  fi

  if ((ART_DEBUG)); then
    {
      echo "[ARTDBG] track: $(display_path "$track")"
      echo "[ARTDBG] candidates (${#detail_lines[@]}):"
      if ((${#detail_lines[@]})); then
        printf '  %s\n' "${detail_lines[@]}"
      else
        echo "  (none)"
      fi
      echo "[ARTDBG] chosen: ${best_src:-none} $(display_path "${best:-}")"
    } >&2
  fi
}

link_cover() {
  local cover="$1" dst_dir="$2"
  [[ -n "$cover" ]] || return 0
  mkdir -p "$dst_dir"
  if [[ "$cover" != "$dst_dir/$COVER_PREFERRED_FILE" ]]; then
    ln -sf -- "$cover" "$dst_dir/$COVER_PREFERRED_FILE"
  fi
  # Ensure only the canonical name is exposed to mpv
  find "$dst_dir" -maxdepth 1 -type l ! -name "$COVER_PREFERRED_FILE" -delete 2>/dev/null || true
}

make_cover_png() {
  local src="$1" dst_dir="$2"
  [[ -n "$src" ]] || return 1
  mkdir -p "$dst_dir"
  local dst="$dst_dir/$COVER_PREFERRED_FILE"

  # If already a PNG, copy/symlink to reduce work
  local ext=${src##*.}
  ext=${ext,,}
  if [[ "$ext" == "png" ]]; then
    if [[ "$src" != "$dst" ]]; then
      ln -sf -- "$src" "$dst"
    fi
    echo "$dst"
    return 0
  fi

  "$IMAGE_EXTRACT_BIN" -loglevel error -y -i "$src" -frames:v 1 "$dst" 2>/dev/null || return 1
  [[ -s "$dst" ]] || return 1
  echo "$dst"
}

strip_embedded_art() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local ext tmp
  ext=${file##*.}
  ext=${ext,,}

  case "$ext" in
  flac)
    if command -v metaflac >/dev/null 2>&1; then
      metaflac --remove --block-type=PICTURE "$file" >/dev/null 2>&1 || true
      return 0
    fi
    ;;
  esac

  tmp=$(mktemp "${file}.noart.XXXXXX") || return 0

  # Drop all video/attachment streams, keep audio and metadata
  if "$IMAGE_EXTRACT_BIN" -loglevel error -nostdin -y -i "$file" -map 0:a -map_metadata 0 -vn -dn -sn -c copy "$tmp" 2>/dev/null; then
    if [[ -s "$tmp" ]]; then
      mv -- "$tmp" "$file"
      return 0
    fi
  fi

  rm -f -- "$tmp" 2>/dev/null || true
}

mpv_send() {
  local json="$1"
  python - "$SOCK" "$json" <<'PY'
import socket, sys

if len(sys.argv) < 3:
    sys.exit(0)

sock_path = sys.argv[1]
payload = sys.argv[2]

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    s.connect(sock_path)
except OSError:
    sys.exit(0)

try:
    s.sendall(payload.encode("utf-8") + b"\n")
    s.shutdown(socket.SHUT_WR)
except OSError:
    s.close()
    sys.exit(0)

out = []
while True:
    try:
        chunk = s.recv(4096)
    except OSError:
        break
    if not chunk:
        break
    out.append(chunk)
s.close()
sys.stdout.write(b"".join(out).decode("utf-8", errors="ignore"))
PY
}

get_playlist_pos() {
  local resp pos
  resp=$(mpv_send '{"command":["get_property","playlist-pos"]}' || true)
  pos=$(sed -n 's/.*"data":[ \t]*\([^,}]*\).*/\1/p' <<<"$resp" | head -n1)
  echo "$pos"
}

get_current_rg_track_gain() {
  local resp gain

  resp=$(mpv_send '{"command":["get_property","current-tracks/audio/replaygain-track-gain"]}' || true)

  gain=$(sed -n 's/.*"data":[ \t]*"\([^"]*\)".*/\1/p' <<<"$resp" | head -n1)

  if [[ -z "$gain" ]]; then
    gain=$(sed -n 's/.*"data":[ \t]*\([-0-9.]\+\).*/\1/p' <<<"$resp" | head -n1)
    if [[ -n "$gain" ]]; then
      gain="${gain} dB"
    fi
  fi

  echo "$gain"
}

get_current_path() {
  local resp path
  resp=$(mpv_send '{"command":["get_property","path"]}' || true)
  path=$(sed -n 's/.*"data":[ \t]*"\([^"]*\)".*/\1/p' <<<"$resp" | head -n1)
  echo "$path"
}

start_mpv() {
  local -a args=()
  args+=("${MPV_USER_ARGS[@]}")
  args+=(--force-window=immediate --idle=yes --keep-open=yes --input-ipc-server="$SOCK")
  args+=(--cover-art-auto=exact)
  if ((NORMALIZE)); then
    args+=(--replaygain=track)
  else
    args+=(--replaygain=no)
  fi

  mpv "${args[@]}" &
  MPV_PID=$!

  for _ in {1..50}; do
    [[ -S "$SOCK" ]] && break
    sleep 0.1
  done
  [[ -S "$SOCK" ]] || die "mpv IPC socket did not appear at $SOCK"

  mpv_send '{"command":["playlist-clear"]}' >/dev/null 2>&1 || true
}

copy_audio() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  cp --reflink=auto -- "$src" "$dst"
}

strip_rg_tags_if_possible() {
  local file="$1"
  local ext
  ext=$(lower_ext "$file")
  if [[ "$ext" != "flac" ]]; then
    return 0
  fi
  if ((METAFLAC_AVAILABLE)); then
    metaflac --remove-replay-gain "$file" 2>/dev/null || true
  else
    maybe_warn_metaflac
  fi
}

add_replaygain_if_requested() {
  local file="$1"
  local ext
  ext=$(lower_ext "$file")
  ((NORMALIZE)) || return 0
  [[ "$ext" == "flac" ]] || return 0
  ((METAFLAC_AVAILABLE)) || return 0
  metaflac --add-replay-gain "$file" 2>/dev/null || true
}

prepare_track() {
  local index="$1"
  local src="$2"
  local dst_dir dst base

  base=$(basename "$src")
  dst_dir="$TMP_ROOT/$index"
  dst="$dst_dir/$base"

  copy_audio "$src" "$dst"
  strip_embedded_art "$dst"
  strip_rg_tags_if_possible "$dst"
  add_replaygain_if_requested "$dst"

  local cover
  select_cover_for_track "$src" "$dst_dir" "$dst" || true
  cover="$COVER_SELECTED_BEST"
  if [[ -n "$cover" ]]; then
    local converted
    converted=$(make_cover_png "$cover" "$dst_dir" || true)
    if [[ -n "$converted" ]]; then
      link_cover "$converted" "$dst_dir"
      cover="$converted"
    else
      link_cover "$cover" "$dst_dir"
    fi
  fi

  COVER_CHOICE_PATHS[$index]="${cover:-}"
  COVER_CHOICE_META[$index]="${COVER_SELECTED_META:-}"
  COVER_CHOICE_DETAIL[$index]="${COVER_SELECTED_DETAIL:-}"

  if ((ART_DEBUG)); then
    printf '[ARTDBG] store idx=%s cover=%s meta=%s detail=%q\n' \
      "$index" "${COVER_CHOICE_PATHS[$index]}" "${COVER_CHOICE_META[$index]}" "${COVER_CHOICE_DETAIL[$index]}" >&2
  fi

  PREPARED_PATH="$dst"
}

append_to_mpv() {
  local file="$1"
  local mode="$2" # replace|append-play
  local json
  json=$(printf '{"command":["loadfile","%s","%s"]}' "$file" "$mode")
  mpv_send "$json" >/dev/null 2>&1 || true
}

print_rg_for_pos() {
  local pos="$1"
  if [[ "${RG_PRINTED_FOR_POS[$pos]:-0}" -ne 0 ]]; then
    return
  fi
  RG_PRINTED_FOR_POS[$pos]=1

  local gain path base msg
  gain=$(get_current_rg_track_gain)
  path=$(get_current_path)
  base=$(basename "${path:-unknown}")

  if [[ -z "$gain" || "$gain" == "null" ]]; then
    msg="ReplayGain[track]: (no RG track gain reported)"
  else
    msg="ReplayGain[track]: $gain"
  fi

  local src_path cover_meta cover_path
  src_path="${TRACKS[$pos]:-unknown}"
  cover_path="${COVER_CHOICE_PATHS[$pos]:-}"
  cover_meta="${COVER_CHOICE_META[$pos]:-}"
  local cover_detail="${COVER_CHOICE_DETAIL[$pos]:-}"
  local src_disp

  # Copy stored cover info into locals to avoid indexing a null/empty array
  local stored_cover="${COVER_CHOICE_PATHS[$pos]+x}"
  local stored_meta="${COVER_CHOICE_META[$pos]+x}"
  local stored_detail="${COVER_CHOICE_DETAIL[$pos]+x}"

  cover_path="${stored_cover:+${COVER_CHOICE_PATHS[$pos]}}"
  cover_meta="${stored_meta:+${COVER_CHOICE_META[$pos]}}"
  cover_detail="${stored_detail:+${COVER_CHOICE_DETAIL[$pos]}}"

  src_disp=$(display_path "$src_path")
  printf '\n\033[33m[RG]\033[0m %s | src: %s\n' "$msg" "$src_disp" >&2
  printf '\033[36m[ART]\033[0m candidates:\n%s\n' "${cover_detail:-"[ ] no images found"}" >&2
  if ((ART_DEBUG)); then
    printf '[ARTDBG] pos=%s meta=%s cover_path=%s detail_lines=%q\n' "$pos" "$cover_meta" "$cover_path" "$cover_detail" >&2
  fi
  printf '%s\n' '----------------------------------------' >&2
}

queue_more() {
  local total="$1"
  local current_pos_ref="$2"
  local highest_appended_ref="$3"
  local next_to_prepare_ref="$4"
  local -n pos="$current_pos_ref"
  local -n high="$highest_appended_ref"
  local -n next="$next_to_prepare_ref"
  local appended=0

  local target=$((pos + BUFFER_AHEAD))
  while ((high < target)); do
    local src prepared

    if ((ALBUM_SPREAD_MODE)); then
      if ((next >= ${#TRACKS[@]})); then
        src=$(next_album_spread_track || true)
        if [[ -z "$src" ]]; then
          break
        fi
        TRACKS+=("$src")
      else
        src="${TRACKS[$next]}"
      fi
    else
      ((next < total)) || break
      src="${TRACKS[$next]}"
    fi

    PREPARED_PATH=""
    prepare_track "$next" "$src"
    prepared="$PREPARED_PATH"
    if ((high < 0)); then
      append_to_mpv "$prepared" "replace"
    else
      append_to_mpv "$prepared" "append-play"
    fi
    high=$next
    next=$((next + 1))
    appended=1
  done

  return $((appended == 0))
}

clean_finished() {
  local upto="$1" last_clean_ref="$2"
  local -n last="$last_clean_ref"
  local i
  if ((upto - 1 > last)); then
    for ((i = last + 1; i < upto; i++)); do
      rm -rf -- "$TMP_ROOT/$i"
      unset TRACKS[$i]
      unset COVER_CHOICE_PATHS[$i]
      unset COVER_CHOICE_META[$i]
      unset COVER_CHOICE_DETAIL[$i]
    done
    last=$((upto - 1))
  fi
}

main() {
  parse_args "$@"
  set_display_root
  check_dependencies

  case "$MODE" in
  random) printf '\033[36m[info]\033[0m Building shuffled list from \033[35m%s\033[0m (this may take a few seconds)\n' "$LIBRARY" ;;
  album) printf '\033[36m[info]\033[0m Preparing album from \033[35m%s\033[0m\n' "$ALBUM_DIR" ;;
  playlist) printf '\033[36m[info]\033[0m Preparing playlist from \033[35m%s\033[0m\n' "$PLAYLIST_FILE" ;;
  esac

  # Start mpv early so the window/IPC socket is up while we scan
  start_mpv

  case "$MODE" in
  random) gather_random_tracks ;;
  album) gather_album_tracks ;;
  playlist) gather_playlist_tracks ;;
  esac

  local total=${#TRACKS[@]}
  if ((ALBUM_SPREAD_MODE)); then
    total=$TOTAL_TRACK_COUNT
  fi

  print_header "$total"

  ensure_tmp_root

  local next_to_prepare=0
  local highest_appended=-1
  local current_pos=-1
  local last_cleaned=-1
  declare -A RG_PRINTED_FOR_POS=()

  queue_more "$total" current_pos highest_appended next_to_prepare

  while :; do
    sleep 5

    if ! kill -0 "$MPV_PID" 2>/dev/null; then
      break
    fi

    local pos
    pos=$(get_playlist_pos)
    if [[ -z "$pos" || "$pos" == "null" ]]; then
      if ((ALBUM_SPREAD_MODE)); then
        if queue_more "$total" current_pos highest_appended next_to_prepare; then
          continue
        else
          break
        fi
      else
        if ((next_to_prepare >= total)); then
          break
        else
          queue_more "$total" current_pos highest_appended next_to_prepare
          continue
        fi
      fi
    fi

    if ! [[ "$pos" =~ ^-?[0-9]+$ ]]; then
      continue
    fi

    local p=$pos
    if ((p != current_pos)); then
      clean_finished "$p" last_cleaned
      current_pos=$p
      print_rg_for_pos "$p"
      if ((ALBUM_SPREAD_MODE)); then
        queue_more "$total" current_pos highest_appended next_to_prepare || break
      else
        queue_more "$total" current_pos highest_appended next_to_prepare
      fi
    fi
  done

  rm -rf -- "$TMP_ROOT"
  wait "$MPV_PID" 2>/dev/null || true
  [[ -S "$SOCK" ]] && rm -f -- "$SOCK" || true
}

main "$@"
