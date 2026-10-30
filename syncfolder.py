#!/usr/bin/env python3
"""
syncfolder - sincronizzazione bidirezionale di una cartella tra 2 PC via HTTP.

Solo libreria standard Python (3.8+), nessuna dipendenza.

  PC A (server + tunnel Cloudflare, stampa l'indirizzo pubblico):
      python3 syncfolder.py server --dir ~/Sync --tunnel

  PC B:
      python3 syncfolder.py client --dir ~/Sync --token SEGRETO \
          --url https://sync.tuodominio.com --interval 10

Il server espone la sua cartella; il client confronta periodicamente la propria
con quella del server e propaga modifiche, nuovi file e cancellazioni in
entrambe le direzioni. In caso di conflitto (file modificato su entrambi i lati)
vince il piu' recente e l'altro viene conservato come "nome.conflict-....ext".
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE = ".syncstate.json"
CACHE_FILE = ".synccache.json"  # cache degli hash scritta dalla pagina web
PART_SUFFIX = ".syncpart"
TMP_SUFFIX = ".synctmp"
IGNORED_NAMES = {STATE_FILE, CACHE_FILE, ".DS_Store", "Thumbs.db", "desktop.ini"}
# Cloudflare (piano free) rifiuta richieste con body > 100 MB: carichiamo a pezzi.
CHUNK_SIZE = 32 * 1024 * 1024


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg, flush=True)


def is_ignored(rel):
    name = rel.rsplit("/", 1)[-1]
    return (name in IGNORED_NAMES or name.endswith(PART_SUFFIX)
            or name.endswith(TMP_SUFFIX))


def safe_path(root, rel):
    """Converte un path relativo (con '/') in assoluto, impedendo di uscire da root."""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or any(p in ("", ".", "..") for p in rel.split("/")):
        raise ValueError("path non valido: %r" % rel)
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([full, root]) != root:
        raise ValueError("path fuori dalla cartella: %r" % rel)
    return full


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


GITIGNORE = ".gitignore"


def gitignore_regex(pat):
    """Traduce un pattern di .gitignore in una regex sul path relativo alla
    cartella del .gitignore. Stessa logica di gitIgnoreRegex() in web.html."""
    anchored = "/" in pat           # con una '/' (non finale) vale solo da quella cartella
    pat = pat.lstrip("/")
    out, i, n = "", 0, len(pat)
    while i < n:
        if pat.startswith("**", i) and (i == 0 or pat[i - 1] == "/"):
            if i + 2 == n:          # "dir/**": tutto quello che c'e' dentro
                out += ".*"
                i += 2
                continue
            if pat[i + 2] == "/":   # "**/": zero o piu' cartelle
                out += "(?:.*/)?"
                i += 3
                continue
        c = pat[i]
        if c == "*":
            out += "[^/]*"
            while i + 1 < n and pat[i + 1] == "*":
                i += 1
        elif c == "?":
            out += "[^/]"
        elif c == "[" and "]" in pat[i + 2:]:
            j = pat.index("]", i + 2)
            body = pat[i + 1:j]
            if body[0] in "!^":
                body = "^" + body[1:]
            out += "[" + body.replace("\\", "\\\\") + "]"
            i = j
        elif c == "\\" and i + 1 < n:
            i += 1
            out += re.escape(pat[i])
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + ("" if anchored else "(?:.*/)?") + out + "$")


class GitIgnore:
    """Regole dei .gitignore di una cartella, anche annidati: ogni file vale per
    la sua cartella e le sottocartelle, e i piu' profondi hanno la precedenza."""

    def __init__(self):
        self.rules = {}  # cartella relativa ("" = radice) -> [(regex, negata, solo_cartelle)]
        self.memo = {}

    def add(self, base, text):
        rules = []
        for line in text.splitlines():
            line = line.rstrip("\r")
            if not line.startswith("\\ "):
                line = line.rstrip(" ")
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            elif line.startswith("\\#") or line.startswith("\\!"):
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if line:
                rules.append((gitignore_regex(line), negate, dir_only))
        self.rules[base] = rules
        self.memo.clear()

    def _match(self, rel, is_dir):
        result = False
        for base in sorted(self.rules, key=lambda b: b.count("/") + (b != "")):
            if base and not rel.startswith(base + "/"):
                continue
            sub = rel[len(base) + 1:] if base else rel
            for regex, negate, dir_only in self.rules[base]:
                if (is_dir or not dir_only) and regex.match(sub):
                    result = not negate
        return result

    def ignored(self, rel, is_dir=False):
        """True se il path (o una cartella che lo contiene) e' escluso."""
        if not self.rules:
            return False
        key = (rel, is_dir)
        if key not in self.memo:
            parent = rel.rpartition("/")[0]
            self.memo[key] = ((bool(parent) and self.ignored(parent, True))
                              or self._match(rel, is_dir))
        return self.memo[key]

    @classmethod
    def from_manifest(cls, manifest):
        """Ricostruisce le regole dal contenuto dei .gitignore inclusi nel manifest."""
        gi = cls()
        for rel, info in manifest.items():
            if "gitignore" in info:
                gi.add(rel.rpartition("/")[0], info["gitignore"])
        return gi


class Scanner:
    """Elenca i file di una cartella con hash, ricalcolando l'hash solo se
    dimensione o mtime sono cambiati. Salta quello che i .gitignore escludono."""

    def __init__(self, root):
        self.root = root
        self.cache = {}  # rel -> (size, mtime_ns, sha)
        self.lock = threading.Lock()
        self.gitignore = GitIgnore()

    def scan(self):
        with self.lock:
            return self._scan()

    def _scan(self):
        result = {}
        self.gitignore = gi = GitIgnore()
        for dirpath, dirnames, filenames in os.walk(self.root):
            base = os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            base = "" if base == "." else base
            prefix = base + "/" if base else ""
            if GITIGNORE in filenames:
                try:
                    with open(os.path.join(dirpath, GITIGNORE), encoding="utf-8",
                              errors="replace") as f:
                        gi.add(base, f.read())
                except OSError:
                    pass
            dirnames[:] = [d for d in dirnames if not d.startswith(".sync")
                           and not gi.ignored(prefix + d, True)]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = prefix + name
                if is_ignored(rel) or os.path.islink(full) or gi.ignored(rel):
                    continue
                try:
                    st = os.stat(full)
                    cached = self.cache.get(rel)
                    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
                        sha = cached[2]
                    else:
                        sha = sha256_file(full)
                        self.cache[rel] = (st.st_size, st.st_mtime_ns, sha)
                except OSError:
                    continue  # file sparito o bloccato durante la scansione
                result[rel] = {"size": st.st_size, "mtime": st.st_mtime, "sha": sha}
                if name == GITIGNORE and base in gi.rules:
                    # l'altro lato usa anche queste regole, per non toccare mai
                    # file che uno dei due lati ignora
                    with open(full, encoding="utf-8", errors="replace") as f:
                        result[rel]["gitignore"] = f.read()
        for rel in list(self.cache):
            if rel not in result:
                del self.cache[rel]
        return result


def write_atomic(dest, src_tmp, mtime):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.utime(src_tmp, (mtime, mtime))
    os.replace(src_tmp, dest)


def remove_file(root, full):
    if os.path.exists(full):
        os.remove(full)
    # rimuove le cartelle rimaste vuote
    d = os.path.dirname(full)
    while d != root and d.startswith(root):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


# --------------------------------------------------------------------- server

def load_web_page():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html"),
              encoding="utf-8") as f:
        return f.read()


def make_handler(root, token, scanner):
    tickets = {}  # ticket monouso per scaricare lo zip -> scadenza
    page_headers = {
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; "
                                   "style-src 'unsafe-inline'; frame-ancestors 'none'",
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "syncfolder/1.0"

        def log_message(self, fmt, *args):
            log("%s %s" % (self.address_string(), fmt % args))

        def _reply(self, code, body=b"", ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _auth_and_parse(self):
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got, "Bearer " + token):
                self._reply(401, {"error": "unauthorized"})
                return None
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if "path" in q:
                try:
                    q["full"] = safe_path(root, q["path"])
                except ValueError as e:
                    self._reply(400, {"error": str(e)})
                    return None
            return u.path, q

        def _read_body(self):
            length = int(self.headers.get("Content-Length", "0"))
            remaining = length
            while remaining > 0:
                block = self.rfile.read(min(remaining, 1024 * 1024))
                if not block:
                    raise IOError("connessione interrotta")
                remaining -= len(block)
                yield block

        def _send_zip(self, query):
            ticket = urllib.parse.parse_qs(query).get("ticket", [""])[0]
            if tickets.pop(ticket, 0) < time.time():
                return self._reply(403, {"error": "link scaduto, riprova dalla pagina"})
            name = os.path.basename(root) or "sync"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s.zip"
                             % urllib.parse.quote(name))
            self.end_headers()
            # niente Content-Length: lo zip viene creato mentre si invia
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:
                for rel in sorted(scanner.scan()):
                    try:
                        z.write(safe_path(root, rel), rel)
                    except OSError:
                        pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path in ("/", "/index.html"):
                # la pagina non contiene segreti: il token lo inserisce l'utente
                return self._reply(200, load_web_page(), "text/html; charset=utf-8",
                                   page_headers)
            if u.path == "/zip":
                return self._send_zip(u.query)
            parsed = self._auth_and_parse()
            if not parsed:
                return
            route, q = parsed
            if route == "/manifest":
                return self._reply(200, scanner.scan())
            if route == "/file" and "full" in q:
                full = q["full"]
                if not os.path.isfile(full):
                    return self._reply(404, {"error": "not found"})
                st = os.stat(full)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(st.st_size))
                self.send_header("X-Mtime", repr(st.st_mtime))
                self.end_headers()
                with open(full, "rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        self.wfile.write(block)
                return
            self._reply(404, {"error": "unknown route"})

        def do_PUT(self):
            # upload a pezzi: PUT /upload?path=..&offset=N  (offset 0 = nuovo file)
            parsed = self._auth_and_parse()
            if not parsed:
                return
            route, q = parsed
            if route != "/upload" or "full" not in q:
                return self._reply(404, {"error": "unknown route"})
            part = q["full"] + PART_SUFFIX
            offset = int(q.get("offset", "0"))
            os.makedirs(os.path.dirname(part), exist_ok=True)
            if offset == 0:
                open(part, "wb").close()
            if os.path.getsize(part) != offset:
                return self._reply(409, {"error": "offset errato"})
            with open(part, "ab") as f:
                for block in self._read_body():
                    f.write(block)
            self._reply(200, {"size": os.path.getsize(part)})

        def do_POST(self):
            # POST /commit?path=..&mtime=..&sha=..  -> rende definitivo l'upload
            parsed = self._auth_and_parse()
            if not parsed:
                return
            route, q = parsed
            if route == "/ticket":
                now = time.time()
                for t, exp in list(tickets.items()):
                    if exp < now:
                        del tickets[t]
                t = secrets.token_urlsafe(24)
                tickets[t] = now + 60
                return self._reply(200, {"ticket": t})
            if route != "/commit" or "full" not in q:
                return self._reply(404, {"error": "unknown route"})
            part = q["full"] + PART_SUFFIX
            if not os.path.isfile(part):
                return self._reply(404, {"error": "nessun upload in corso"})
            if sha256_file(part) != q.get("sha"):
                os.remove(part)
                return self._reply(422, {"error": "hash non corrispondente"})
            write_atomic(q["full"], part, float(q["mtime"]))
            self._reply(200, {"ok": True})

        def do_DELETE(self):
            parsed = self._auth_and_parse()
            if not parsed:
                return
            route, q = parsed
            if route != "/file" or "full" not in q:
                return self._reply(404, {"error": "unknown route"})
            remove_file(root, q["full"])
            self._reply(200, {"ok": True})

    return Handler


TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def run_tunnel(port, token, stop):
    """Avvia un quick tunnel Cloudflare verso il server e ne stampa l'indirizzo.
    Se cloudflared si chiude lo riavvia (l'indirizzo pero' cambia)."""
    exe = shutil.which("cloudflared")
    if not exe:
        log("cloudflared non trovato: installalo (brew install cloudflared) o avvia senza --tunnel")
        return
    while not stop.is_set():
        proc = subprocess.Popen(
            [exe, "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:%d" % port],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        stop.proc = proc
        for line in proc.stderr:  # cloudflared scrive tutto su stderr
            m = TUNNEL_URL_RE.search(line)
            if m:
                url = m.group(0)
                link = "%s/#token=%s" % (url, urllib.parse.quote(token, safe=""))
                bar = "=" * (len(link) + 4)
                log("tunnel attivo: %s\n\n"
                    "  Link di accesso (contiene il token, condividilo solo con chi deve entrare):\n"
                    "  %s\n  %s\n  %s\n" % (url, bar, link, bar))
            elif " ERR " in line:
                log("cloudflared: " + line.strip())
        proc.wait()
        if not stop.is_set():
            log("cloudflared terminato (codice %s), riavvio tra 5 secondi..." % proc.returncode)
            stop.wait(5)


def run_server(args):
    if not args.token:
        # nuovo a ogni avvio: i link degli avvii precedenti smettono di funzionare
        args.token = secrets.token_urlsafe(32)
        log("generato un nuovo token per questa sessione")
    if not args.tunnel:
        log("link di accesso locale: http://127.0.0.1:%d/#token=%s"
            % (args.port, urllib.parse.quote(args.token, safe="")))
    root = os.path.realpath(os.path.expanduser(args.dir))
    os.makedirs(root, exist_ok=True)
    handler = make_handler(root, args.token, Scanner(root))
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    log("server in ascolto su http://%s:%d  cartella: %s" % (args.host, args.port, root))
    stop = threading.Event()
    stop.proc = None
    if args.tunnel:
        log("avvio il tunnel Cloudflare...")
        threading.Thread(target=run_tunnel, args=(args.port, args.token, stop), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if stop.proc and stop.proc.poll() is None:
            stop.proc.terminate()


# --------------------------------------------------------------------- client

class Remote:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token,
                        "User-Agent": "syncfolder/1.0"}

    def _req(self, method, route, params=None, data=None):
        url = self.url + route
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=300)

    def manifest(self):
        with self._req("GET", "/manifest") as r:
            return json.load(r)

    def download(self, rel, dest_tmp):
        with self._req("GET", "/file", {"path": rel}) as r, open(dest_tmp, "wb") as f:
            mtime = float(r.headers.get("X-Mtime", time.time()))
            for block in iter(lambda: r.read(1024 * 1024), b""):
                f.write(block)
        return mtime

    def upload(self, rel, full, info):
        offset = 0
        with open(full, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk and offset > 0:
                    break
                self._req("PUT", "/upload", {"path": rel, "offset": offset}, chunk).close()
                offset += len(chunk)
                if len(chunk) < CHUNK_SIZE:
                    break
        self._req("POST", "/commit", {"path": rel, "mtime": repr(info["mtime"]),
                                      "sha": info["sha"]}, b"").close()

    def delete(self, rel):
        self._req("DELETE", "/file", {"path": rel}).close()


def conflict_name(rel):
    base, ext = os.path.splitext(rel)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    host = socket.gethostname().split(".")[0]
    return "%s.conflict-%s-%s%s" % (base, host, stamp, ext)


class Client:
    def __init__(self, root, remote):
        self.root = root
        self.remote = remote
        self.scanner = Scanner(root)
        self.state_path = os.path.join(root, STATE_FILE)
        try:
            with open(self.state_path) as f:
                self.state = json.load(f)  # rel -> sha dell'ultima sync riuscita
        except (OSError, ValueError):
            self.state = {}

    def manifests(self):
        """File locali e del server, togliendo quelli esclusi dai .gitignore
        di uno qualsiasi dei due lati. Restituisce anche la funzione di filtro."""
        local = self.scanner.scan()
        remote = self.remote.manifest()
        mine, theirs = self.scanner.gitignore, GitIgnore.from_manifest(remote)

        def skip(rel):
            return mine.ignored(rel) or theirs.ignored(rel)

        return ({r: v for r, v in local.items() if not skip(r)},
                {r: v for r, v in remote.items() if not skip(r)}, skip)

    def one_way(self, direction, mirror=False):
        """direction "down": server -> locale, "up": locale -> server.
        Copia i file nuovi o diversi; con mirror cancella anche quelli in piu'
        sulla destinazione."""
        local, remote, _ = self.manifests()
        src, dst = (remote, local) if direction == "down" else (local, remote)
        todo = sorted(r for r in src if src[r]["sha"] != (dst.get(r) or {}).get("sha"))
        extra = sorted(r for r in dst if r not in src) if mirror else []
        for rel in src:
            if rel not in todo:
                self.state[rel] = src[rel]["sha"]
        total = len(todo) + len(extra)
        errors = 0
        for rel in todo + extra:
            try:
                full = safe_path(self.root, rel)
                if rel in src:
                    if direction == "down":
                        log("↓ download %s" % rel)
                        self.pull(rel)
                    else:
                        log("↑ upload   %s" % rel)
                        self.remote.upload(rel, full, src[rel])
                    self.state[rel] = src[rel]["sha"]
                else:
                    if direction == "down":
                        log("✗ cancella in locale   %s" % rel)
                        remove_file(self.root, full)
                    else:
                        log("✗ cancella sul server  %s" % rel)
                        self.remote.delete(rel)
                    self.state.pop(rel, None)
            except (OSError, urllib.error.URLError, ValueError) as e:
                errors += 1
                log("errore su %s: %s" % (rel, e))
        self.save_state()
        return total - errors

    def save_state(self):
        tmp = self.state_path + TMP_SUFFIX
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    def pull(self, rel):
        full = safe_path(self.root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + TMP_SUFFIX
        try:
            mtime = self.remote.download(rel, tmp)
            write_atomic(full, tmp, mtime)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def sync_once(self):
        local, remote, skip = self.manifests()
        changes = 0
        for rel in sorted(set(local) | set(remote) | set(self.state)):
            if is_ignored(rel) or skip(rel):
                continue
            L = local.get(rel)
            R = remote.get(rel)
            ls = L["sha"] if L else None
            rs = R["sha"] if R else None
            base = self.state.get(rel)
            try:
                full = safe_path(self.root, rel)
                if ls == rs:
                    final = ls
                elif rs == base:            # cambiato solo in locale
                    if L:
                        log("↑ upload   %s" % rel)
                        self.remote.upload(rel, full, L)
                    else:
                        log("✗ cancella sul server  %s" % rel)
                        self.remote.delete(rel)
                    final = ls
                elif ls == base:            # cambiato solo sul server
                    if R:
                        log("↓ download %s" % rel)
                        self.pull(rel)
                    else:
                        log("✗ cancella in locale   %s" % rel)
                        remove_file(self.root, full)
                    final = rs
                # da qui: cambiato su entrambi i lati (conflitto)
                elif not R:                 # cancellato la', modificato qui: si tiene
                    log("! conflitto (cancellato sul server) -> ricarico %s" % rel)
                    self.remote.upload(rel, full, L)
                    final = ls
                elif not L:                 # cancellato qui, modificato la': si tiene
                    log("! conflitto (cancellato in locale) -> riscarico %s" % rel)
                    self.pull(rel)
                    final = rs
                else:
                    # vince il piu' recente; l'altra versione resta come copia
                    # di conflitto, che verra' sincronizzata al prossimo giro
                    cfull = safe_path(self.root, conflict_name(rel))
                    if L["mtime"] >= R["mtime"]:
                        log("! conflitto %s: vince locale, versione server -> %s"
                            % (rel, os.path.basename(cfull)))
                        tmp = cfull + TMP_SUFFIX
                        write_atomic(cfull, tmp, self.remote.download(rel, tmp))
                        self.remote.upload(rel, full, L)
                        final = ls
                    else:
                        log("! conflitto %s: vince server, versione locale -> %s"
                            % (rel, os.path.basename(cfull)))
                        os.replace(full, cfull)
                        self.pull(rel)
                        final = rs
                if ls != rs:
                    changes += 1
                if final:
                    self.state[rel] = final
                else:
                    self.state.pop(rel, None)
            except (OSError, urllib.error.URLError, ValueError) as e:
                log("errore su %s: %s (riprovo al prossimo giro)" % (rel, e))
        self.save_state()
        return changes


def run_client(args):
    root = os.path.realpath(os.path.expanduser(args.dir))
    os.makedirs(root, exist_ok=True)
    client = Client(root, Remote(args.url, args.token))
    if args.mode != "sync":
        n = client.one_way(args.mode, args.mirror)
        log("%s completato: %d file" % ("download" if args.mode == "down" else "upload", n))
        return
    log("sync %s <-> %s ogni %ss" % (root, args.url, args.interval))
    while True:
        try:
            n = client.sync_once()
            if n:
                log("sincronizzati %d file" % n)
        except urllib.error.HTTPError as e:
            log("errore HTTP %s: %s" % (e.code, e.read()[:200]))
            if e.code == 401:
                sys.exit("token errato")
        except (OSError, urllib.error.URLError, ValueError) as e:
            log("server non raggiungibile: %s" % e)
        if args.once:
            break
        time.sleep(args.interval)


def main():
    p = argparse.ArgumentParser(description="Sync bidirezionale di una cartella via HTTP")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("server", help="espone la cartella (PC con il tunnel)")
    s.add_argument("--dir", required=True)
    s.add_argument("--token", default=os.environ.get("SYNC_TOKEN"),
                   help="token fisso (default: uno nuovo generato a ogni avvio)")
    s.add_argument("--host", default="127.0.0.1",
                   help="127.0.0.1 = raggiungibile solo dal tunnel (default)")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--tunnel", action="store_true",
                   help="avvia anche 'cloudflared tunnel --url' e stampa l'indirizzo pubblico")

    c = sub.add_parser("client", help="sincronizza una cartella locale col server")
    c.add_argument("--dir", required=True)
    c.add_argument("--url", required=True)
    c.add_argument("--token", default=os.environ.get("SYNC_TOKEN"))
    c.add_argument("--interval", type=float, default=10)
    c.add_argument("--once", action="store_true", help="una sola sincronizzazione")
    c.add_argument("--mode", choices=("sync", "down", "up"), default="sync",
                   help="down = solo scarica, up = solo carica (una volta)")
    c.add_argument("--mirror", action="store_true",
                   help="con down/up elimina sulla destinazione i file che non ci sono nella sorgente")

    args = p.parse_args()
    if args.token is not None and len(args.token) < 16:
        p.error("il token deve avere almeno 16 caratteri")
    if args.cmd == "client" and not args.token:
        p.error("serve --token (o SYNC_TOKEN) di almeno 16 caratteri")
    (run_server if args.cmd == "server" else run_client)(args)


if __name__ == "__main__":
    main()
