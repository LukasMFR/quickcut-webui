#!/usr/bin/env bash

# === quickcut.sh — découpe rapide de segments vidéo (ffmpeg) ===
# Usage: ./quickcut.sh <video.mp4>
# - Demande le nombre de segments puis start/end pour chacun (UI identique)
# - EXÉCUTION OPTIMISÉE: lance les exports en PARALLÈLE (jusqu'au nb de CPU)
# - Pas de ré-encodage: ultra rapide, qualité identique (-c copy)
# - NEW: les extraits héritent des dates Finder (Création/Modifié) recalculées avec les timecodes

# ----- Couleurs & UI -----
BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; RESET="$(printf '\033[0m')"
RED="$(printf '\033[31m')"; GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"; CYAN="$(printf '\033[36m')"

banner() {
  echo ""
  echo "${BOLD}🎬  QUICKCUT — Cutter express (ffmpeg)${RESET}"
  echo "${DIM}Astuce: formats temps acceptés  mm:ss  ou  hh:mm:ss (ex: 0:12, 01:12:03)${RESET}"
  echo ""
}

rule() {
  local cols="${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}"
  printf "${CYAN}%*s${RESET}\n" "$cols" "" | tr ' ' '='
}

die() { echo "${RED}❌ $*${RESET}"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

time_to_seconds() {
  # Supporte "SS", "MM:SS", "HH:MM:SS"
  local t="$1" hh=0 mm=0 ss=0 IFS=:
  read -r a b c <<<"$t"
  if [[ -z "$b" && -z "$c" ]]; then
    ss="$a"
  elif [[ -z "$c" ]]; then
    mm="$a"; ss="$b"
  else
    hh="$a"; mm="$b"; ss="$c"
  fi
  hh="${hh//[^0-9]/}"; mm="${mm//[^0-9]/}"; ss="${ss//[^0-9]/}"
  echo $((10#$hh*3600 + 10#$mm*60 + 10#$ss))
}

safe_time_for_name() { echo "$1" | tr ':' '-'; }

# ----- Checks -----
banner
rule
have ffmpeg || die "ffmpeg introuvable. Installe-le avec: ${YELLOW}brew install ffmpeg${RESET}"

INPUT="$1"
[[ -n "$INPUT" ]] || die "Usage: ${CYAN}$0 <video.mp4>${RESET}"
[[ -f "$INPUT" ]] || die "Fichier introuvable: ${YELLOW}$INPUT${RESET}"

INPUT_ABS="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"
STEM="$(basename "${INPUT_ABS%.*}")"
BASEDIR="$(dirname "$INPUT_ABS")"
OUTDIR="${BASEDIR}/${STEM}_cuts"

# Concurrence auto: utilise 100% des CPU dispo
MAXJOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
(( MAXJOBS < 1 )) && MAXJOBS=1

# NEW: on lit les dates Finder du fichier source
# %B = birth/creation time (epoch), %m = modification time (epoch)
SRC_BIRTH_EPOCH="$(stat -f %B "$INPUT_ABS" 2>/dev/null)"
SRC_MOD_EPOCH="$(stat -f %m "$INPUT_ABS" 2>/dev/null)"
# Fallbacks si non dispo (exFAT/FAT peuvent retourner 0)
[[ -z "$SRC_BIRTH_EPOCH" || "$SRC_BIRTH_EPOCH" -le 0 ]] && SRC_BIRTH_EPOCH="$SRC_MOD_EPOCH"

echo "📄 Fichier source : ${CYAN}$INPUT_ABS${RESET}"
echo "📂 Dossier sortie (si >1 segment) : ${CYAN}$OUTDIR${RESET}"
echo "🧠 Concurrence    : ${CYAN}${MAXJOBS} job(s) en parallèle${RESET}"
if [[ -n "$SRC_BIRTH_EPOCH" && "$SRC_BIRTH_EPOCH" -gt 0 ]]; then
  echo "🕒 Création source: ${CYAN}$(date -r "$SRC_BIRTH_EPOCH" "+%Y-%m-%d %H:%M:%S")${RESET}"
  echo "🕒 Modifié source : ${CYAN}$(date -r "$SRC_MOD_EPOCH"   "+%Y-%m-%d %H:%M:%S")${RESET}"
fi
rule

# Nombre de segments
while true; do
  read -rp "✂️  Nombre de segments à extraire : " NUM
  [[ "$NUM" =~ ^[0-9]+$ ]] && (( NUM > 0 )) && break
  echo "${YELLOW}⚠️  Entre un entier > 0${RESET}"
done
echo ""
echo "${BOLD}OK, on prépare ${NUM} segment(s).${RESET}"
rule

# On collecte d'abord tous les segments, puis on lance en parallèle
declare -a STARTS ENDS OUTFILES STARTSECS ENDSECS
i=1
while (( i <= NUM )); do
  echo "${BOLD}— Segment #$i —${RESET}"

  # Prompts simples
  read -rp "  ⏱️  Début  (ex 0:12 ou 00:00:12) : " START
  read -rp "  ⏱️  Fin    (ex 0:17 ou 00:00:17) : " END

  SSEC="$(time_to_seconds "$START")" || SSEC=-1
  ESEC="$(time_to_seconds "$END")"   || ESEC=-1
  if (( SSEC < 0 || ESEC < 0 || ESEC <= SSEC )); then
    echo "${RED}❌ Temps invalides (fin doit être > début). On recommence ce segment.${RESET}"
    rule
    continue
  fi

  START_TAG="$(safe_time_for_name "$START")"
  END_TAG="$(safe_time_for_name "$END")"

  if (( NUM == 1 )); then
    # 1 segment → pas de dossier, pas de "partXX" dans le nom
    OUTFILE="${BASEDIR}/${STEM}__${START_TAG}-${END_TAG}.mp4"
  else
    OUTFILE="${OUTDIR}/${STEM}_part$(printf '%02d' "$i")__${START_TAG}-${END_TAG}.mp4"
  fi

  STARTS+=("$START"); ENDS+=("$END")
  OUTFILES+=("$OUTFILE")
  STARTSECS+=("$SSEC"); ENDSECS+=("$ESEC")
  ((i++))
  rule
done

echo "${BOLD}🚀 Lancement des exports en parallèle…${RESET}"
# Création du dossier uniquement si >1 segments
if (( NUM > 1 )); then
  mkdir -p "$OUTDIR" || die "Impossible de créer ${OUTDIR}"
fi

# Attendre un créneau dans le pool
wait_for_slot() {
  while (( $(jobs -pr | wc -l | tr -d ' ') >= MAXJOBS )); do
    sleep 0.1
  done
}

shopt -s nullglob
for idx in "${!OUTFILES[@]}"; do
  START="${STARTS[$idx]}"
  END="${ENDS[$idx]}"
  OUTFILE="${OUTFILES[$idx]}"
  SSEC="${STARTSECS[$idx]}"
  ESEC="${ENDSECS[$idx]}"

  # NEW: calcule les dates cibles pour l’extrait
  # - Création extrait  = Création source + start_offset
  # - Modifié extrait   = Création source + end_offset
  # (logique: premier et dernier frame du segment)
  PART_CRT_EPOCH=$(( SRC_BIRTH_EPOCH + SSEC ))
  PART_MOD_EPOCH=$(( SRC_BIRTH_EPOCH + ESEC ))
  (( PART_MOD_EPOCH < PART_CRT_EPOCH )) && PART_MOD_EPOCH="$PART_CRT_EPOCH"

  # Formats utiles
  CT_ISO_UTC="$(date -u -r "$PART_CRT_EPOCH" "+%Y-%m-%dT%H:%M:%SZ")"
  TOUCH_MOD="$(date -r "$PART_MOD_EPOCH" "+%Y%m%d%H%M.%S")"
  # SetFile (si dispo) prend "MM/DD/YYYY HH:MM:SS" en localtime
  SETFILE_CRT="$(date -r "$PART_CRT_EPOCH" "+%m/%d/%Y %H:%M:%S")"

  wait_for_slot
  {
    # on écrit aussi la métadonnée MP4 creation_time pour cohérence
    if ffmpeg -nostdin -hide_banner -loglevel error -y \
              -ss "$START" -to "$END" -i "$INPUT_ABS" \
              -metadata creation_time="$CT_ISO_UTC" \
              -c copy "$OUTFILE"; then
      # maj des dates Finder
      touch -t "$TOUCH_MOD" "$OUTFILE" 2>/dev/null
      if have SetFile; then
        SetFile -d "$SETFILE_CRT" "$OUTFILE" 2>/dev/null
      fi
      echo "✅ Créé → ${GREEN}${OUTFILE}${RESET}"
    else
      echo "❌ ${RED}Échec ffmpeg${RESET} pour $START → $END"
    fi
  } &
  echo "🧵 Job lancé: ${CYAN}$START → $END${RESET} → ${GREEN}$(basename "$OUTFILE")${RESET}"
done

wait  # attend la fin de tous les jobs
rule
echo "${BOLD}🎉 Terminé !${RESET}"

# Récap (sans logs)
if (( NUM == 1 )); then
  if [[ -f "${OUTFILES[0]}" ]]; then
    echo "  • ${GREEN}${OUTFILES[0]}${RESET}"
  else
    echo "  ${RED}Aucun fichier créé.${RESET}"
  fi
else
  CREATED=( "$OUTDIR"/"${STEM}_part"*.mp4 )
  if ((${#CREATED[@]}==0)); then
    echo "  ${RED}Aucun segment généré.${RESET}"
  else
    for f in "${CREATED[@]}"; do
      echo "  • ${GREEN}$f${RESET}"
    done
  fi
fi

echo ""
echo "${DIM}Glisse ces clips dans Final Cut (import « laisser à l’emplacement actuel »).${RESET}"
