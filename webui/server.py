#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import re
import mimetypes
import subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, send_from_directory, jsonify, Response

# === Config ===
PORT = int(os.environ.get("QUICKCUT_PORT", "5050"))
MAX_WORKERS = os.cpu_count() or 4
APP = Flask(__name__, static_folder="static", static_url_path="/static")

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


# ---------- Utils ----------
def is_mac() -> bool:
    return sys.platform == "darwin"


def has_cmd(cmd: str) -> bool:
    return subprocess.call(
        ["/usr/bin/env", "bash", "-lc", f"command -v {cmd} >/dev/null 2>&1"]
    ) == 0


def fmt_for_setfile(epoch: int) -> str:
    # "MM/DD/YYYY HH:MM:SS" (localtime) pour SetFile
    return datetime.fromtimestamp(epoch).strftime("%m/%d/%Y %H:%M:%S")


def fmt_for_touch(epoch: int) -> str:
    # "YYYYmmddHHMM.SS" (localtime) pour touch -t
    return datetime.fromtimestamp(epoch).strftime("%Y%m%d%H%M.%S")


def fmt_iso_utc(epoch: int) -> str:
    # MP4 metadata creation_time (UTC)
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def birth_mtime(path: str) -> tuple[int, int]:
    st = os.stat(path)
    # macOS: st_birthtime, sinon fallback sur mtime
    birth = getattr(st, "st_birthtime", 0) or int(st.st_mtime)
    mod = int(st.st_mtime)
    return int(birth), int(mod)


def stem_from_path(path: str) -> str:
    b = os.path.basename(path)
    return os.path.splitext(b)[0]


def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


def parse_timecode(tc: str) -> int:
    # "SS" | "MM:SS" | "HH:MM:SS"
    parts = tc.strip().split(":")
    if len(parts) == 1:
        return int(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    raise ValueError("Bad timecode")


def safe_time_for_name(tc: str) -> str:
    return tc.replace(":", "-")


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        ext = os.path.splitext(path)[1].lower()
        if ext in {".mp4", ".m4v"}:
            mime = "video/mp4"
        elif ext == ".mov":
            mime = "video/quicktime"
        else:
            mime = "application/octet-stream"
    return mime


def _parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    """
    Retourne (start, end) inclusifs pour 'Range: bytes=start-end'
    Supporte 'start-' et '-suffix'. Lève ValueError si invalide.
    """
    m = re.match(r"bytes=(\d*)-(\d*)", range_header or "")
    if not m:
        raise ValueError("Bad Range header")
    g1, g2 = m.groups()
    if g1 == "" and g2 == "":
        raise ValueError("Empty range")

    if g1 == "":  # suffix: last N bytes
        length = int(g2)
        if length <= 0:
            raise ValueError("Bad suffix length")
        start = max(0, file_size - length)
        end = file_size - 1
    else:
        start = int(g1)
        end = file_size - 1 if g2 == "" else int(g2)
        if start >= file_size or start > end:
            raise ValueError("Out of range")
        end = min(end, file_size - 1)
    return start, end


def run_ffmpeg_segment(input_path: str, start: str, end: str, out_path: str, creation_epoch: int):
    # Calcule les dates pour ce segment
    ssec = parse_timecode(start)
    esec = parse_timecode(end)
    crt_epoch = creation_epoch + ssec
    mod_epoch = max(creation_epoch + esec, crt_epoch)

    # Ajoute la métadonnée MP4 creation_time (UTC) et copie sans réencodage
    ct_iso = fmt_iso_utc(crt_epoch)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", start, "-to", end, "-i", input_path,
        "-metadata", f"creation_time={ct_iso}",
        "-c", "copy", out_path
    ]
    subprocess.check_call(cmd)

    # MAJ des dates Finder
    # mtime
    subprocess.call(["touch", "-t", fmt_for_touch(mod_epoch), out_path])
    # birth/creation (si SetFile dispo)
    if is_mac() and has_cmd("SetFile"):
        subprocess.call(["SetFile", "-d", fmt_for_setfile(crt_epoch), out_path])

    return {
        "output": out_path,
        "creation_time": ct_iso,
        "birth": datetime.fromtimestamp(crt_epoch).isoformat(sep=" ", timespec="seconds"),
        "modified": datetime.fromtimestamp(mod_epoch).isoformat(sep=" ", timespec="seconds"),
    }


# ---------- Routes ----------
@APP.route("/")
def index():
    return send_from_directory(APP.static_folder, "index.html")


# --- Boîte de dialogue Finder native (macOS) ---
@APP.get("/api/choose-file")
def api_choose_file():
    if not is_mac():
        return jsonify({"ok": False, "error": "macOS uniquement pour cette action."}), 400

    # Une seule ligne pour éviter l'erreur de syntaxe AppleScript.
    # On filtre par extensions pour compatibilité (mp4, mov, m4v).
    script = '''
        try
            set f to choose file with prompt "Sélectionnez une vidéo" of type {"mp4","mov","m4v"}
            POSIX path of f
        on error number -128
            return "USER_CANCELED"
        end try
    '''
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        out = (proc.stdout or "").strip()
        if out == "USER_CANCELED":
            return jsonify({"ok": False, "canceled": True})
        if proc.returncode != 0 or not out:
            return jsonify({"ok": False, "error": (proc.stderr or "Échec AppleScript").strip()}), 500
        return jsonify({"ok": True, "path": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# --- Streaming local avec support Range robuste (+ arrêt silencieux si le client coupe) ---
@APP.get("/api/stream")
def api_stream():
    path = request.args.get("path", "")
    if not path or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "Invalid path"}), 400

    file_size = os.path.getsize(path)
    mime = _guess_mime(path)
    range_header = request.headers.get("Range")

    # Détermine la plage
    status = 200
    start, end = 0, file_size - 1
    if range_header:
        try:
            start, end = _parse_range(range_header, file_size)
            status = 206
        except ValueError:
            return Response(status=416)  # Range Not Satisfiable

    length = end - start + 1
    chunk_size = 1024 * 1024  # 1 MiB

    def generate():
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    read_len = chunk_size if remaining > chunk_size else remaining
                    data = f.read(read_len)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Le client a coupé la connexion → on arrête sans bruit
            return

    rv = Response(generate(), status=status, mimetype=mime, direct_passthrough=True)
    rv.headers["Accept-Ranges"] = "bytes"
    rv.headers["Content-Length"] = str(length)
    if status == 206:
        rv.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    # éviter le cache pendant l’édition
    rv.headers["Cache-Control"] = "no-store"
    return rv


@APP.route("/api/cut", methods=["POST"])
def api_cut():
    data = request.get_json(force=True)
    src = data.get("path", "")
    segments = data.get("segments", [])
    trash = bool(data.get("trashOriginal", False))

    if not src or not os.path.isfile(src):
        return jsonify({"ok": False, "error": "Invalid path"}), 400
    if not segments:
        return jsonify({"ok": False, "error": "No segments"}), 400

    # Dates de base
    birth_epoch, _ = birth_mtime(src)
    stem = stem_from_path(src)
    basedir = os.path.dirname(src)
    outdir = os.path.join(basedir, f"{stem}_cuts") if len(segments) > 1 else basedir

    if len(segments) > 1:
        ensure_dir(outdir)

    futures = []
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        for i, seg in enumerate(segments, start=1):
            start = seg["start"].strip()
            end = seg["end"].strip()
            start_tag = safe_time_for_name(start)
            end_tag = safe_time_for_name(end)
            if len(segments) == 1:
                outfile = os.path.join(outdir, f"{stem}__{start_tag}-{end_tag}.mp4")
            else:
                outfile = os.path.join(outdir, f"{stem}_part{str(i).zfill(2)}__{start_tag}-{end_tag}.mp4")
            fut = exe.submit(run_ffmpeg_segment, src, start, end, outfile, birth_epoch)
            futures.append(fut)

        for fut in as_completed(futures):
            try:
                results.append({"ok": True, **fut.result()})
            except subprocess.CalledProcessError as e:
                results.append({"ok": False, "error": f"ffmpeg failed: {e}"})
            except Exception as e:
                results.append({"ok": False, "error": str(e)})

    # Mise à la corbeille ?
    if trash and all(r.get("ok") for r in results):
        try:
            from send2trash import send2trash
            send2trash(src)
            trashed = True
        except Exception as e:
            trashed = False
            results.append({"ok": False, "error": f"trash failed: {e}"})
    else:
        trashed = False

    return jsonify({"ok": True, "results": results, "trashedOriginal": trashed})


@APP.route("/api/reveal", methods=["POST"])
def api_reveal():
    data = request.get_json(force=True)
    path = data.get("path", "")
    if not path or not os.path.exists(path):
        return jsonify({"ok": False, "error": "Invalid path"}), 400
    try:
        if is_mac():
            subprocess.call(["open", "-R", path])
        else:
            subprocess.call(["xdg-open", os.path.dirname(path)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Fichiers statiques (sert /static/*)
@APP.route("/<path:path>")
def static_proxy(path):
    return send_from_directory(APP.static_folder, path)


if __name__ == "__main__":
    print(f"QuickCut WebUI → http://127.0.0.1:{PORT}")
    APP.run(host="127.0.0.1", port=PORT, debug=True)
