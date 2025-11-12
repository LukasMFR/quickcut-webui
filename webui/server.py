#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from mimetypes import guess_type
from flask import Flask, request, send_from_directory, jsonify, Response, send_file

# === Config ===
PORT = int(os.environ.get("QUICKCUT_PORT", "5050"))
MAX_WORKERS = os.cpu_count() or 4
APP = Flask(__name__, static_folder="static", static_url_path="/static")

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}

def is_mac() -> bool:
    return sys.platform == "darwin"

def has_cmd(cmd: str) -> bool:
    return subprocess.call(["/usr/bin/env", "bash", "-lc", f"command -v {cmd} >/dev/null 2>&1"]) == 0

def fmt_for_setfile(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%m/%d/%Y %H:%M:%S")

def fmt_for_touch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y%m%d%H%M.%S")

def fmt_iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def birth_mtime(path: str) -> tuple[int, int]:
    st = os.stat(path)
    birth = getattr(st, "st_birthtime", 0) or int(st.st_mtime)
    mod = int(st.st_mtime)
    return int(birth), int(mod)

def stem_from_path(path: str) -> str:
    b = os.path.basename(path)
    return os.path.splitext(b)[0]

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def parse_timecode(tc: str) -> int:
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

def run_ffmpeg_segment(input_path: str, start: str, end: str, out_path: str, creation_epoch: int):
    ssec = parse_timecode(start)
    esec = parse_timecode(end)
    crt_epoch = creation_epoch + ssec
    mod_epoch = max(creation_epoch + esec, crt_epoch)

    ct_iso = fmt_iso_utc(crt_epoch)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", start, "-to", end, "-i", input_path,
        "-metadata", f"creation_time={ct_iso}",
        "-c", "copy", out_path
    ]
    subprocess.check_call(cmd)

    subprocess.call(["touch", "-t", fmt_for_touch(mod_epoch), out_path])
    if is_mac() and has_cmd("SetFile"):
        subprocess.call(["SetFile", "-d", fmt_for_setfile(crt_epoch), out_path])

    return {
        "output": out_path,
        "creation_time": ct_iso,
        "birth": datetime.fromtimestamp(crt_epoch).isoformat(sep=" ", timespec="seconds"),
        "modified": datetime.fromtimestamp(mod_epoch).isoformat(sep=" ", timespec="seconds"),
    }

# --------- Routes ---------

@APP.route("/")
def index():
    return send_from_directory(APP.static_folder, "index.html")

# --- NOUVEAU : boîte de dialogue Finder native (macOS) ---
@APP.route("/api/choose-file")
def api_choose_file():
    if not is_mac():
        return jsonify({"ok": False, "error": "macOS uniquement pour cette action."}), 400
    script = r'''
        set t to {"public.movie","public.mpeg-4","com.apple.quicktime-movie"}
        set f to choose file with prompt "Sélectionnez une vidéo" of type t
        POSIX path of f
    '''
    try:
        out = subprocess.check_output(["osascript", "-e", script], text=True).strip()
        return jsonify({"ok": True, "path": out})
    except subprocess.CalledProcessError:
        # utilisateur a cliqué sur Annuler
        return jsonify({"ok": False, "canceled": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# --- NOUVEAU : streaming local avec support Range pour la prévisualisation ---
@APP.route("/api/stream")
def api_stream():
    path = request.args.get("path", "")
    if not path or not os.path.isfile(path):
        return "Not found", 404

    mime = guess_type(path)[0] or "application/octet-stream"
    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range", None)

    if not range_header:
        # Réponse complète
        resp = send_file(path, mimetype=mime, conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    # Ex: "bytes=0-1023"
    try:
        bytes_unit, rng = range_header.split("=")
        start_s, end_s = (rng.split("-") + [""])[:2]
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
        end = min(end, file_size - 1)
        start = max(0, start)
        if start > end:
            start = 0
        length = end - start + 1
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(length)
        rv = Response(data, 206, mimetype=mime, direct_passthrough=True)
        rv.headers.add("Content-Range", f"bytes {start}-{end}/{file_size}")
        rv.headers.add("Accept-Ranges", "bytes")
        rv.headers.add("Content-Length", str(length))
        return rv
    except Exception:
        # fallback : envoie complet
        resp = send_file(path, mimetype=mime, conditional=True)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

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

@APP.route("/<path:path>")
def static_proxy(path):
    return send_from_directory(APP.static_folder, path)

if __name__ == "__main__":
    print(f"QuickCut WebUI → http://127.0.0.1:{PORT}")
    APP.run(host="127.0.0.1", port=PORT, debug=True)
