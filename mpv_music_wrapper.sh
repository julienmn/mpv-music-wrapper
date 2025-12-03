#!/usr/bin/env bash
set -euo pipefail

# mpv_music_wrapper.sh
# Modes:
#   --random-mode=full-library --library /path/to/library [--normalize]
#   --album /path/to/album [--normalize]
#   --playlist /path/to/playlist.m3u [--normalize]
# Only one mode at a time. Extra args are forwarded to mpv.

PID=$$
SOCK="/tmp/mpv-${PID}.sock"
TMP_ROOT=""
BUFFER_AHEAD=1

AUDIO_EXTS=(flac mp3 ogg opus m4a alac wav aiff wv)
PLAYLIST_EXTS=(m3u m3u8 pls cue)
IMAGE_EXTS=(jpg jpeg png webp gif bmp tiff tif svg)
PREFERRED_IMAGE_KEYWORDS=(cover front folder)
IMAGE_PROBE_BIN="ffprobe"
IMAGE_EXTRACT_BIN="ffmpeg"
COVER_PREFERRED_FILE="cover.png"
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

log_info() { printf '[info] %s\n' "$*"; }
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
  --library <dir>              Required for --random-mode. Not used by other modes.
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

gather_random_tracks() {
  mapfile -d '' -t TRACKS < <(
    find "$LIBRARY" -type f -print0 |
      while IFS= read -r -d '' f; do
        if is_audio "$f"; then
          printf '%s\0' "$f"
        fi
      done |
      shuf -z
  )
  ((${#TRACKS[@]} > 0)) || die "No audio files found under $LIBRARY"
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
  local best="" best_area=-1 best_kw_match=0 best_kw_rank=999 best_name="" best_src="external" best_w=0 best_h=0 best_size=-1
  local -a detail_lines=()

  COVER_SELECTED_META=""
  COVER_SELECTED_DETAIL="[ ] no images found"
  COVER_SELECTED_BEST=""

  dir=$(dirname "$track")
  mapfile -d '' -t candidates < <(find_images_recursive "$dir" || true)

  embedded=$(extract_embedded_cover "$audio_copy" "$dst_dir" 2>/dev/null || true)
  if [[ -n "$embedded" ]]; then
    candidates+=("$embedded")
  fi

  for f in "${candidates[@]}"; do
    dims=$(image_dims_area "$f")
    read -r w h area <<<"$dims"
    size=$(stat -c %s "$f" 2>/dev/null || echo 0)
    kw=0
    kw_rank=999
    lower=$(basename "$f")
    lower=${lower,,}
    local idx=0
    for kwd in "${PREFERRED_IMAGE_KEYWORDS[@]}"; do
      if [[ "$lower" == *"$kwd"* ]]; then
        kw=1
        kw_rank=$idx
        break
      fi
      idx=$((idx + 1))
    done
    name=$(basename "$f")
    if [[ -n "$embedded" && "$f" == "$embedded" ]]; then
      src_type="embedded"
      disp_path="(embedded from $(display_path "$track"))"
    else
      src_type="external"
      disp_path=$(display_path "$f")
    fi
    detail_lines+=("path=$disp_path src=$src_type res=${w}x${h} area=$area size=$size kw=$kw")

    if { ((kw == 1 && best_kw_match == 0)); } ||
      { ((kw == 1 && best_kw_match == 1)) && ((area > best_area)); } ||
      { ((kw == 1 && best_kw_match == 1)) && ((area == best_area)) && ((kw_rank < best_kw_rank)); } ||
      { ((kw == 1 && best_kw_match == 1)) && ((area == best_area)) && ((kw_rank == best_kw_rank)) && ((size > best_size)); } ||
      { ((kw == 1 && best_kw_match == 1)) && ((area == best_area)) && ((kw_rank == best_kw_rank)) && ((size == best_size)) && { [[ -z "$best_name" ]] || [[ "$name" < "$best_name" ]]; }; } ||
      { ((kw == 0 && best_kw_match == 0)) && ((area > best_area)); } ||
      { ((kw == 0 && best_kw_match == 0)) && ((area == best_area)) && ((size > best_size)); } ||
      { ((kw == 0 && best_kw_match == 0)) && ((area == best_area)) && ((size == best_size)) && { [[ -z "$best_name" ]] || [[ "$name" < "$best_name" ]]; }; }; then
      best="$f"
      best_area=$area
      best_kw_match=$kw
      best_kw_rank=$kw_rank
      best_name="$name"
      best_w=$w
      best_h=$h
      best_size=$size
      best_src=$src_type
    fi
  done

  if [[ -n "$embedded" && -n "$best" && "$embedded" != "$best" ]]; then
    rm -f -- "$embedded"
  fi

  if [[ -n "$best" ]]; then
    COVER_SELECTED_META="${best_src}|${best_w}|${best_h}|${best_area}|${best_kw_match}|${best_size}"
    local formatted=()
    for line in "${detail_lines[@]}"; do
      if [[ "$line" == path="$(display_path "$best")"* && "$best_src" == "external" ]]; then
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

  local target=$((pos + BUFFER_AHEAD))
  while ((high < target && next < total)); do
    local prepared
    PREPARED_PATH=""
    prepare_track "$next" "${TRACKS[$next]}"
    prepared="$PREPARED_PATH"
    if ((high < 0)); then
      append_to_mpv "$prepared" "replace"
    else
      append_to_mpv "$prepared" "append-play"
    fi
    high=$next
    next=$((next + 1))
  done
}

clean_finished() {
  local upto="$1" last_clean_ref="$2"
  local -n last="$last_clean_ref"
  local i
  if ((upto - 1 > last)); then
    for ((i = last + 1; i < upto; i++)); do
      rm -rf -- "$TMP_ROOT/$i"
    done
    last=$((upto - 1))
  fi
}

main() {
  parse_args "$@"
  set_display_root
  check_dependencies

  case "$MODE" in
    random) printf '\033[36m[info]\033[0m Building shuffled list from \033[35m%s\033[0m (this may take a few seconds)\n' "$LIBRARY";;
    album) printf '\033[36m[info]\033[0m Preparing album from \033[35m%s\033[0m\n' "$ALBUM_DIR";;
    playlist) printf '\033[36m[info]\033[0m Preparing playlist from \033[35m%s\033[0m\n' "$PLAYLIST_FILE";;
  esac

  case "$MODE" in
  random) gather_random_tracks ;;
  album) gather_album_tracks ;;
  playlist) gather_playlist_tracks ;;
  esac

  local total=${#TRACKS[@]}

  # Fancy header around startup info (colored)
  local mode_line="" path_line=""
  case "$MODE" in
    random)
      mode_line="Mode: random"
      path_line="Library: $LIBRARY"
      ;;
    album)
      mode_line="Mode: album"
      path_line="Album: $ALBUM_DIR"
      ;;
    playlist)
      mode_line="Mode: playlist"
      path_line="Playlist: $PLAYLIST_FILE"
      ;;
    *)
      mode_line="Mode: unknown"
      path_line=""
      ;;
  esac

  local -a header_lines=()
  header_lines+=("mpv music wrapper")
  header_lines+=("$mode_line")
  if [[ -n "$path_line" ]]; then
    header_lines+=("$path_line")
  fi
  header_lines+=("Socket: $SOCK")
  header_lines+=("Tracks: $total")
  header_lines+=("Buffer ahead: $BUFFER_AHEAD")
  if ((NORMALIZE)); then
    header_lines+=("Normalize: enabled (ReplayGain track)")
  else
    header_lines+=("Normalize: disabled")
  fi

  local max_len=0 line
  for line in "${header_lines[@]}"; do
    (( ${#line} > max_len )) && max_len=${#line}
  done
  local inner_width=$max_len

  # Build top/bottom borders dynamically
  printf '\033[36m╔' >&2
  printf '═%.0s' $(seq 1 $((inner_width + 2))) >&2
  printf '╗\033[0m\n' >&2

  for line in "${header_lines[@]}"; do
    printf '\033[36m║\033[0m %-*s \033[36m║\033[0m\n' "$inner_width" "$line"
  done

  printf '\033[36m╚' >&2
  printf '═%.0s' $(seq 1 $((inner_width + 2))) >&2
  printf '╝\033[0m\n' >&2

  ensure_tmp_root

  start_mpv

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
      if ((next_to_prepare >= total)); then
        break
      else
        queue_more "$total" current_pos highest_appended next_to_prepare
        continue
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
    queue_more "$total" current_pos highest_appended next_to_prepare
  fi
  done

  rm -rf -- "$TMP_ROOT"
  wait "$MPV_PID" 2>/dev/null || true
  [[ -S "$SOCK" ]] && rm -f -- "$SOCK" || true
}

main "$@"
