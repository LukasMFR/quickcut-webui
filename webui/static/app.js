// Helpers
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function fmtTime(t) { // seconds -> HH:MM:SS or MM:SS
  t = Math.max(0, Math.floor(t));
  const h = Math.floor(t/3600), m = Math.floor((t%3600)/60), s = t%60;
  const pad = (n)=>String(n).padStart(2,"0");
  return h>0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
function parseTime(tc) { // "SS" | "MM:SS" | "HH:MM:SS" -> seconds
  const p = tc.split(":").map(x=>parseInt(x,10));
  if (p.some(isNaN)) return NaN;
  if (p.length===1) return p[0];
  if (p.length===2) return p[0]*60 + p[1];
  if (p.length===3) return p[0]*3600 + p[1]*60 + p[2];
  return NaN;
}

// State
let IN = null, OUT = null;
let segments = [];

const player = $("#player");
const fileLocal = $("#fileLocal");
const absPath = $("#absPath");
const inOutDisplay = $("#inOutDisplay");
const tbody = $("#segmentsTable tbody");
const output = $("#cutOutput");
const trashOriginal = $("#trashOriginal");

// ==== File preview (no upload) ====
fileLocal.addEventListener("change", () => {
  const f = fileLocal.files[0];
  if (!f) return;
  const url = URL.createObjectURL(f);
  player.src = url;
  player.play().catch(()=>{});
});

// ==== Markers & controls ====
function refreshMarkers() {
  const i = IN==null ? "—" : fmtTime(IN);
  const o = OUT==null ? "—" : fmtTime(OUT);
  inOutDisplay.textContent = `In: ${i} | Out: ${o}`;
}
$("#markIn").addEventListener("click", ()=> { IN = player.currentTime; refreshMarkers(); });
$("#markOut").addEventListener("click", ()=> { OUT = player.currentTime; refreshMarkers(); });

document.addEventListener("keydown", (e)=>{
  if (e.target.matches("input, textarea")) return;
  if (e.code==="Space") { e.preventDefault(); if (player.paused) player.play(); else player.pause(); }
  if (e.key==="i" || e.key==="I") { IN = player.currentTime; refreshMarkers(); }
  if (e.key==="o" || e.key==="O") { OUT = player.currentTime; refreshMarkers(); }
  if (e.key==="Enter") { e.preventDefault(); addSegment(); }
  if (e.key==="ArrowLeft")  { player.currentTime = Math.max(0, player.currentTime - (e.shiftKey?5:0.5)); }
  if (e.key==="ArrowRight") { player.currentTime = player.currentTime + (e.shiftKey?5:0.5); }
});

function addSegment() {
  if (IN==null || OUT==null || OUT<=IN) { alert("Définis d'abord un In et un Out valides."); return; }
  segments.push({ start: fmtTime(IN), end: fmtTime(OUT) });
  IN = OUT = null; refreshMarkers();
  renderSegments();
}
$("#addSegment").addEventListener("click", addSegment);
$("#clearSegments").addEventListener("click", ()=>{ segments = []; renderSegments(); });

function renderSegments() {
  tbody.innerHTML = "";
  segments.forEach((seg, idx)=>{
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${idx+1}</td>
      <td><input value="${seg.start}" data-idx="${idx}" data-k="start"/></td>
      <td><input value="${seg.end}" data-idx="${idx}" data-k="end"/></td>
      <td><button data-del="${idx}">Supprimer</button></td>
    `;
    tbody.appendChild(tr);
  });
}
tbody.addEventListener("input", (e)=>{
  const t = e.target;
  if (t.tagName!=="INPUT") return;
  const idx = parseInt(t.dataset.idx,10), k = t.dataset.k;
  segments[idx][k] = t.value.trim();
});
tbody.addEventListener("click", (e)=>{
  const b = e.target.closest("button[data-del]");
  if (!b) return;
  const idx = parseInt(b.dataset.del,10);
  segments.splice(idx,1);
  renderSegments();
});

// ==== Explorer (backend) ====
const browser = $("#browser");
const entries = $("#entries");
const cwdLabel = $("#cwdLabel");
$("#browseBtn").addEventListener("click", ()=> {
  browser.classList.remove("hidden");
  loadList("");
});
$("#browserClose").addEventListener("click", ()=> browser.classList.add("hidden"));

async function loadList(path) {
  const resp = await fetch(`/api/list${path?`?path=${encodeURIComponent(path)}`:""}`);
  const data = await resp.json();
  entries.innerHTML = "";
  if (data.error) {
    entries.textContent = data.error;
    return;
  }
  cwdLabel.textContent = data.cwd || "(Racines)";
  if (data.parent) {
    const up = document.createElement("div");
    up.className = "entry";
    up.innerHTML = `<div><strong>⬆️ Dossier parent</strong></div><div class="type">${data.parent}</div>`;
    up.onclick = ()=> loadList(data.parent);
    entries.appendChild(up);
  }
  (data.items||[]).forEach(it=>{
    const div = document.createElement("div");
    div.className = "entry";
    if (it.type==="dir") {
      div.innerHTML = `<div><strong>📁 ${it.name}</strong></div><div class="type">${it.path}</div>`;
      div.onclick = ()=> loadList(it.path);
    } else {
      div.innerHTML = `<div><strong>🎞️ ${it.name}</strong></div><div class="type">${it.path}</div>`;
      div.onclick = ()=>{
        absPath.value = it.path;
        browser.classList.add("hidden");
      };
    }
    entries.appendChild(div);
  });
}

// ==== Lancer la découpe ====
$("#cutButton").addEventListener("click", async ()=>{
  const path = absPath.value.trim();
  if (!path) { alert("Renseigne le chemin absolu du fichier à découper."); return; }
  if (segments.length===0) { alert("Ajoute au moins un segment."); return; }
  // validation rapide
  for (const s of segments) {
    if (isNaN(parseTime(s.start)) || isNaN(parseTime(s.end)) || parseTime(s.end)<=parseTime(s.start)) {
      alert(`Segment invalide: ${s.start} -> ${s.end}`);
      return;
    }
  }
  output.textContent = "Découpe en cours…";

  const resp = await fetch("/api/cut", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ path, segments, trashOriginal: trashOriginal.checked })
  });
  const data = await resp.json();
  if (!data.ok) {
    output.textContent = `Erreur: ${data.error || "inconnue"}`;
    return;
  }
  let txt = "";
  for (const r of data.results) {
    if (r.ok) {
      txt += `✅ ${r.output}\n     creation_time=${r.creation_time} | Birth=${r.birth} | Modified=${r.modified}\n`;
    } else {
      txt += `❌ ${r.error}\n`;
    }
  }
  if (data.trashedOriginal) txt += `\n🗑️ Original envoyé à la corbeille.\n`;
  output.textContent = txt;

  // Liens "Révéler dans Finder"
  // Ajouter boutons dynamiquement
  const lines = data.results.filter(r=>r.ok).map(r=>r.output);
  if (lines.length) {
    const frag = document.createDocumentFragment();
    lines.forEach(p=>{
      const btn = document.createElement("button");
      btn.textContent = "Révéler dans le Finder";
      btn.style.marginRight = "8px";
      btn.onclick = async ()=>{
        await fetch("/api/reveal", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ path: p }) });
      };
      frag.appendChild(btn);
    });
    output.appendChild(document.createElement("div")).appendChild(frag);
  }
});
