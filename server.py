#!/usr/bin/env python3
"""Pembersih Scan: aplikasi web lokal untuk membuang halaman kosong
(hasil scan bolak-balik yang hanya berisi bayangan lubang perforasi)."""

import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageOps

HOST, PORT = "127.0.0.1", int(os.environ.get("PEMBERSIH_PORT", 8765))
APP_DIR = Path(__file__).resolve().parent
WORK = None  # folder kerja sementara, dibuat saat server mulai


def app_version():
    """Penanda versi: waktu ubah terakhir file aplikasi. Dipakai peluncur untuk mendeteksi server lama."""
    return str(max(int(f.stat().st_mtime) for f in APP_DIR.iterdir()
                   if f.suffix in (".py", ".html", ".css")))


VERSION = app_version()

ANALYZE_DPI = 50
THUMB_WIDTH = 150
# Area tengah yang dianalisis; pinggir kiri/kanan diabaikan karena ada bayangan lubang
CROP = (0.12, 0.03, 0.88, 0.97)
DARK_LEVEL = 200  # piksel lebih gelap dari ini dihitung sebagai tinta

IMAGE_DPI = 150      # resolusi halaman hasil konversi gambar
IMAGE_MARGIN = 36    # margin (pt) untuk tata letak "pas dengan margin", 36 pt = 1,27 cm
A4 = (595.0, 842.0)

jobs = {}
jobs_lock = threading.Lock()


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def ink_percent(img):
    w, h = img.size
    c = img.crop((int(w * CROP[0]), int(h * CROP[1]), int(w * CROP[2]), int(h * CROP[3])))
    hist = c.histogram()
    total = sum(hist)
    return 100.0 * sum(hist[:DARK_LEVEL]) / total if total else 0.0


def analyze(job_id, pdf_path):
    jdir = pdf_path.parent
    render = jdir / "render"
    render.mkdir()
    run(["pdftoppm", "-r", str(ANALYZE_DPI), "-gray", str(pdf_path), str(render / "p")])
    pages = []
    for i, f in enumerate(sorted(render.glob("p-*.pgm")), start=1):
        img = Image.open(f).convert("L")
        ink = ink_percent(img)
        thumb = img.copy()
        thumb.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 2))
        thumb.save(jdir / f"thumb-{i}.png")
        f.unlink()
        pages.append({"n": i, "ink": round(ink, 3)})
    return pages


def list_dirs(path):
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"Folder tidak ditemukan: {p}")
    subs = []
    for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
        try:
            if c.is_dir() and not c.name.startswith("."):
                subs.append(c.name)
        except PermissionError:
            pass
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
            "dirs": subs, "pdfs": list_pdfs(p)}


def list_pdfs(folder):
    """Nama file PDF langsung di dalam folder (subfolder tidak ikut)."""
    try:
        return sorted((c.name for c in Path(folder).iterdir()
                       if c.is_file() and c.suffix.lower() == ".pdf" and not c.name.startswith(".")),
                      key=str.lower)
    except PermissionError:
        return []


def new_job(name, write_input, src=None):
    job_id = uuid.uuid4().hex
    jdir = WORK / job_id
    jdir.mkdir()
    pdf = jdir / "input.pdf"
    write_input(pdf)
    pages = analyze(job_id, pdf)
    with jobs_lock:
        jobs[job_id] = {"dir": str(jdir), "name": name, "total": len(pages), "src": src}
    return {"id": job_id, "name": name, "pages": pages}


def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise ValueError("Data file sudah tidak ada, silakan masukkan ulang.")
    return job


def page_file(job, n):
    """Path PDF satu halaman (halaman ke-n) dari sebuah job."""
    jdir = Path(job["dir"])
    split = jdir / "split"
    with jobs_lock:
        if not split.exists():
            tmp = jdir / "split-tmp"
            tmp.mkdir(exist_ok=True)
            run(["pdfseparate", str(jdir / "input.pdf"), str(tmp / "pg-%04d.pdf")])
            tmp.rename(split)
    return str(split / f"pg-{int(n):04d}.pdf")


def output_target(outdir, name, overwrite, src=None):
    out = Path(outdir).expanduser().resolve()
    if not out.is_dir():
        raise ValueError(f"Folder tujuan tidak ditemukan: {out}")
    target = out / name
    if src and target == Path(src):
        raise ValueError("Folder tujuan sama dengan folder asal, file asli akan tertimpa. Pilih folder lain.")
    return target, target.exists() and not overwrite


def write_pdf(parts, target):
    tmp_out = WORK / f"hasil-{uuid.uuid4().hex}.pdf"
    try:
        if len(parts) == 1:
            shutil.copy(parts[0], tmp_out)
        else:
            run(["pdfunite", *parts, str(tmp_out)])
        shutil.copy(tmp_out, target)
    finally:
        tmp_out.unlink(missing_ok=True)


def save_clean(job_id, keep, outdir, overwrite):
    job = get_job(job_id)
    keep = sorted({int(k) for k in keep})
    if not keep:
        raise ValueError("Tidak ada halaman yang dipilih untuk disimpan.")
    target, exists = output_target(outdir, job["name"], overwrite, job.get("src"))
    if exists:
        return {"exists": True, "target": str(target)}
    write_pdf([page_file(job, n) for n in keep], target)
    return {"saved": True, "target": str(target), "pages": len(keep), "total": job["total"]}


# ---------- Susun Halaman ----------

def page_size(pdf):
    out = subprocess.run(["pdfinfo", "-f", "1", "-l", "1", str(pdf)], capture_output=True, text=True).stdout
    m = re.search(r"Page\s+1 size:\s+([\d.]+) x ([\d.]+)", out)
    return (float(m[1]), float(m[2])) if m else A4


def image_to_pdf(data, out_pdf, size_pt, layout):
    """Jadikan gambar satu halaman PDF seukuran size_pt, gambar di tengah tanpa terpotong."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)  # foto HP yang miring jadi tegak
    except Exception:
        raise ValueError("Format file tidak didukung. Gunakan PDF, JPG, atau PNG.")
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    W, H = (round(v / 72 * IMAGE_DPI) for v in size_pt)
    margin = 0 if layout == "full" else round(IMAGE_MARGIN / 72 * IMAGE_DPI)
    scale = min((W - 2 * margin) / img.width, (H - 2 * margin) / img.height)
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    page = Image.new("RGB", (W, H), "white")
    page.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    page.save(out_pdf, "PDF", resolution=IMAGE_DPI, quality=85)


def add_source(name, data, ref_id, layout):
    if data[:5] == b"%PDF-":
        return new_job(name, lambda pdf: pdf.write_bytes(data))
    size = A4
    if ref_id:
        size = page_size(Path(get_job(ref_id)["dir"]) / "input.pdf")
    res = new_job(name, lambda pdf: image_to_pdf(data, pdf, size, layout))
    res["image"] = True
    return res


def save_compose(pages, outdir, name, overwrite):
    if not pages:
        raise ValueError("Dokumen kosong, tidak ada halaman untuk disimpan.")
    name = os.path.basename(name.strip())
    if not name:
        raise ValueError("Nama file belum diisi.")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    target, exists = output_target(outdir, name, overwrite)
    if exists:
        return {"exists": True, "target": str(target)}
    write_pdf([page_file(get_job(job_id), n) for job_id, n in pages], target)
    return {"saved": True, "target": str(target), "pages": len(pages)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        data = Path(path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                self.send_file(APP_DIR / "index.html", "text/html; charset=utf-8")
            elif url.path == "/style.css":
                self.send_file(APP_DIR / "style.css", "text/css; charset=utf-8")
            elif url.path == "/susun":
                self.send_file(APP_DIR / "susun.html", "text/html; charset=utf-8")
            elif url.path == "/api/ping":
                self.send_json({"ok": True, "version": VERSION})
            elif url.path == "/api/version":
                self.send_json({"running": VERSION, "files": app_version()})
            elif url.path == "/api/dirs":
                self.send_json(list_dirs(q.get("path", ["~"])[0]))
            elif url.path.startswith("/api/thumb/"):
                job_id, n = url.path.split("/")[3:5]
                with jobs_lock:
                    job = jobs[job_id]
                self.send_file(Path(job["dir"]) / f"thumb-{int(n)}.png", "image/png")
            elif url.path.startswith("/api/preview/"):
                job_id, n = url.path.split("/")[3:5]
                n = int(n)
                with jobs_lock:
                    job = jobs[job_id]
                jdir = Path(job["dir"])
                big = jdir / f"big-{n}.png"
                if not big.exists():
                    run(["pdftoppm", "-r", "110", "-f", str(n), "-l", str(n), "-png",
                         "-singlefile", str(jdir / "input.pdf"), str(jdir / f"big-{n}")])
                self.send_file(big, "image/png")
            else:
                self.send_error(404)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/analyze":
                name = os.path.basename(unquote(self.headers.get("X-Filename", "scan.pdf")))
                body = self.read_body()
                self.send_json(new_job(name, lambda pdf: pdf.write_bytes(body)))
            elif url.path == "/api/analyze-path":
                src = Path(json.loads(self.read_body())["path"]).expanduser().resolve()
                if not src.is_file():
                    raise ValueError(f"File tidak ditemukan: {src}")
                self.send_json(new_job(src.name, lambda pdf: shutil.copy(src, pdf), str(src)))
            elif url.path == "/api/save":
                d = json.loads(self.read_body())
                self.send_json(save_clean(d["id"], d["keep"], d["outdir"], d.get("overwrite", False)))
            elif url.path == "/api/source":
                name = os.path.basename(unquote(self.headers.get("X-Filename", "file")))
                self.send_json(add_source(name, self.read_body(), self.headers.get("X-Ref"),
                                          self.headers.get("X-Layout", "fit")))
            elif url.path == "/api/compose":
                d = json.loads(self.read_body())
                self.send_json(save_compose(d["pages"], d["outdir"], d["name"], d.get("overwrite", False)))
            elif url.path == "/api/mkdir":
                d = json.loads(self.read_body())
                name = d["name"].strip()
                if not name or "/" in name or name in (".", ".."):
                    raise ValueError("Nama folder tidak valid.")
                new = Path(d["path"]).expanduser().resolve() / name
                new.mkdir(exist_ok=True)
                self.send_json({"path": str(new)})
            elif url.path == "/api/shutdown":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown).start()
            elif url.path == "/api/forget":
                d = json.loads(self.read_body())
                with jobs_lock:
                    job = jobs.pop(d["id"], None)
                if job:
                    shutil.rmtree(job["dir"], ignore_errors=True)
                self.send_json({"ok": True})
            else:
                self.send_error(404)
        except subprocess.CalledProcessError as e:
            self.send_json({"error": "Gagal memproses PDF: " + e.stderr.decode(errors="ignore").strip()}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)


def main():
    for tool in ("pdftoppm", "pdfseparate", "pdfunite"):
        if not shutil.which(tool):
            sys.exit(f"Butuh '{tool}' (paket poppler). Install dulu: sudo pacman -S poppler")
    global WORK
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Aplikasi sepertinya sudah berjalan. Membuka {url}")
        webbrowser.open(url)
        return
    WORK = Path(tempfile.mkdtemp(prefix="pembersih-scan-"))
    # Ctrl+C, kill, atau terminal ditutup: hentikan server dengan rapi supaya folder sementara terhapus
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"Pembersih Scan berjalan di {url}", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        shutil.rmtree(WORK, ignore_errors=True)
        print("Berhenti.", flush=True)


if __name__ == "__main__":
    main()
