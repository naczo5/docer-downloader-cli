#!/usr/bin/env python3
"""docer-downloader-cli: paste a docer or scribd link, get the file.

Usage:
  python docer_dl.py "https://docer.pl/doc/s00nxc5"
  python docer_dl.py "https://docer.pl/doc/s00nxc5" -o out.pdf
  python docer_dl.py "https://www.scribd.com/document/55949937/33-Strategies-of-War"

  After `pip install .` (or `pip install git+https://github.com/naczo5/docer-downloader-cli`):
  docer-dl "https://docer.pl/doc/s00nxc5"

  Zero-install with uv:
  uvx --from git+https://github.com/naczo5/docer-downloader-cli docer-dl "https://docer.pl/doc/s00nxc5"
"""
import argparse
import html
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.parse

try:
    import requests
except ImportError:
    sys.exit("missing dependency: pip install -r requirements.txt")

from scribd_dl import ScribdError, download_scribd

DOCER_DOMAINS = ("docer.pl", "docer.ar", "docer.com.ar", "docero.de", "doceru.com")
SCRIBD_DOMAINS = ("scribd.com",)
SUPPORTED_DOMAINS = DOCER_DOMAINS + SCRIBD_DOMAINS
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}


def is_block_page(html_text: str) -> bool:
    return ("Przez roboty internetowe" in html_text
            or "/streaming/captcha.png" in html_text)


def parse_args():
    p = argparse.ArgumentParser(description="Download documents from docer.* and scribd.com by pasting the link")
    p.add_argument("url", help="document URL, e.g. https://docer.pl/doc/s00nxc5 or a scribd.com document link")
    p.add_argument("-o", "--output", default=None, help="output file path (default: auto from title)")
    p.add_argument("--captcha-token", default=None,
                   help="reCAPTCHA token if server demands captcha (solve in browser, pass token here)")
    p.add_argument("--captcha-image", default="docer-captcha.png",
                   help="where to save the anti-bot captcha image (default: docer-captcha.png in cwd)")
    p.add_argument("--no-open", action="store_true",
                   help="don't auto-open the captcha image in the system viewer")
    p.add_argument("--no-progress", action="store_true", help="disable progress output")
    return p.parse_args()


def normalize_url(url: str):
    url = url.strip().strip("'\"")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    parts = urllib.parse.urlparse(url)
    domain = parts.netloc.lower().split(":")[0].removeprefix("www.")
    if domain in SCRIBD_DOMAINS:
        return "scribd", domain, None, url
    if domain not in DOCER_DOMAINS:
        sys.exit(f"unsupported domain '{parts.netloc}'. Supported: {', '.join(SUPPORTED_DOMAINS)}")
    m = re.search(r"/doc/([A-Za-z0-9]+)", parts.path)
    if not m:
        sys.exit("could not find document id in URL (expected .../doc/<id>)")
    doc_id = m.group(1)
    base = f"https://{domain}"
    page_url = f"{base}/doc/{doc_id}"
    return "docer", domain, doc_id, page_url


def extract_attrs(page_html: str, doc_id_fallback: str):
    """Pull data-id / data-ext / data-size + title from page HTML (no bs4 needed)."""
    def find_tag(tag_id):
        m = re.search(rf'<[^>]*id="{tag_id}"[^>]*>', page_html)
        return m.group(0) if m else None

    def attr(tag, name):
        if not tag:
            return None
        m = re.search(rf'{name}="([^"]*)"', tag)
        return html.unescape(m.group(1)) if m else None

    tag2 = find_tag("iframe2")
    tag1 = find_tag("iframe1")
    tag = tag2 or tag1
    item_id = attr(tag, "data-id") or doc_id_fallback
    ext = attr(tag, "data-ext") or "pdf"
    size = attr(tag, "data-size") or ""
    # title lives in <h1 class="margin-0">Title</h1>; block page has a different h1
    title = item_id
    m = re.search(r'<h1[^>]*class="margin-0"[^>]*>(.*?)</h1>', page_html, re.DOTALL)
    if not m:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.DOTALL)
    if m:
        t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())
        # ignore the anti-bot h1
        if t and "roboty internetowe" not in t:
            # strip logo-link h1s (they contain no text)
            if len(t) > 2:
                title = t
    return item_id, ext, size, title


def sanitize(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", name)[:120] or "document"


def resolve_direct_url(url: str) -> str:
    """Unwrap google-viewer URLs (?url=...) if the backend returns one."""
    if "url=" in url:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "url" in q and q["url"][0].startswith("http"):
                return q["url"][0]
        except Exception:
            pass
        m = re.search(r"url=([^&]+)", url)
        if m:
            return urllib.parse.unquote(m.group(1))
    return url


def terminal_file_link(path: str) -> str:
    """Clickable file link: OSC 8 hyperlink (clickable in supporting terminals).

    Terminals without support show the plain absolute path instead.
    """
    abs_path = os.path.abspath(path)
    uri = "file://" + urllib.parse.quote(abs_path)
    return f"\x1b]8;;{uri}\x1b\\{abs_path}\x1b]8;;\x1b\\"


def open_in_viewer(path: str) -> bool:
    """Best-effort auto-open of an image in the OS default viewer.

    Never raises; returns True if an opener was launched.
    """
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if os.name == "nt":
            os.startfile(path)  # noqa: PGH121
            return True
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        pass
    return False


def solve_image_captcha(session, base: str, page_url: str,
                        image_path: str = "docer-captcha.png",
                        auto_open: bool = True) -> bool:
    """Handle docer's 410 anti-bot page: fetch captcha.png, ask user, POST answer.

    The image is auto-opened in the system viewer (unless --no-open) and a
    clickable file:// link is printed, so no file-manager hunting is needed.

    Returns True if the follow-up GET looks unblocked.
    """
    try:
        r = session.get(f"{base}/streaming/captcha.png", timeout=30,
                        headers={"Referer": page_url})
    except Exception as e:
        print(f"[-] could not fetch captcha image: {e}")
        return False
    if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image"):
        print(f"[-] captcha fetch failed: HTTP {r.status_code}")
        return False
    with open(image_path, "wb") as f:
        f.write(r.content)
    print("[!] anti-bot block hit.")
    print(f"    captcha image: {terminal_file_link(image_path)}")
    if auto_open:
        if open_in_viewer(image_path):
            print("    (opened in your image viewer — type the code below)")
        else:
            print("    (could not auto-open; click the path above to open it)")
    else:
        print("    (auto-open disabled; click the path above to open it)")
    try:
        code = input("    captcha code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[-] aborted.")
        return False
    if not code:
        return False
    resp = session.post(page_url, data={"cd_type": "1", "captcha": code},
                        headers={"Referer": page_url}, timeout=30)
    if is_block_page(resp.text):
        print("[-] wrong code or still blocked, try again.")
        return False
    print("[+] captcha accepted.")
    try:
        os.remove(image_path)
    except OSError:
        pass
    return True


def main():
    args = parse_args()
    kind, domain, doc_id, page_url = normalize_url(args.url)

    if kind == "scribd":
        try:
            download_scribd(page_url, output=args.output, no_progress=args.no_progress)
        except ScribdError as e:
            sys.exit(f"[-] {e}")
        return

    base = f"https://{domain}"
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)

    print(f"[+] fetching page: {page_url}")
    r = s.get(page_url, timeout=30)
    # 410 = anti-bot block; solve up to 3 times
    for _ in range(3):
        if r.status_code == 410 or (r.status_code == 200 and is_block_page(r.text)):
            print(f"[-] server returned {r.status_code} (anti-bot).")
            if not solve_image_captcha(s, base, page_url,
                                       image_path=args.captcha_image,
                                       auto_open=not args.no_open):
                break
            r = s.get(page_url, timeout=30)
            continue
        break
    if r.status_code != 200:
        sys.exit(f"page fetch failed: HTTP {r.status_code} (server is rate-limiting bots; "
                 "wait a few minutes and try again)")
    if is_block_page(r.text):
        sys.exit("still blocked by anti-bot captcha. Wait a few minutes and retry.")
    item_id, ext, size, title = extract_attrs(r.text, doc_id)
    print(f"[+] document: {title} (id={item_id} ext={ext} size={size})")

    if args.captcha_token:
        rc = f"{args.captcha_token}{random.randint(1, 9)}"
    else:
        rc = random.randint(1, 9)

    print("[+] requesting direct file URL (/start/show) ...")
    api = f"{base}/start/show"
    resp = s.post(api, data={"item_id": item_id, "rc": rc, "ext": ext, "size": size},
                  headers={"Referer": page_url, "Origin": base,
                           "X-Requested-With": "XMLHttpRequest",
                           "Accept": "application/json, text/javascript, */*; q=0.01"},
                  timeout=30)
    if resp.status_code == 410 or (resp.headers.get("Content-Type", "").startswith("text/html")):
        sys.exit(f"blocked by anti-bot on API call (HTTP {resp.status_code}). "
                 "Wait a few minutes (IP rate-limit) or solve the image captcha "
                 "in a browser once, then retry.")
    try:
        data = resp.json()
    except Exception:
        sys.exit(f"unexpected API response: HTTP {resp.status_code} body={resp.text[:300]}")

    if not data.get("success"):
        print("[-] server refused without captcha.")
        print("    1. open the link in a browser, solve the captcha once")
        print("    2. re-run with --captcha-token <token from grecaptcha>")
        print(f"    raw response: {resp.text[:500]}")
        sys.exit(1)

    file_url = resolve_direct_url(data["response"]["url"])
    real_ext = data["response"].get("extension") or ext
    print(f"[+] direct URL: {file_url}")

    out = args.output
    if not out:
        out = f"{sanitize(title)}.{real_ext}"
        # avoid ugly super-long names, fall back to id if title empty
        if out.startswith(".") or out == f".{real_ext}":
            out = f"{item_id}.{real_ext}"
    if os.path.isdir(out):
        out = os.path.join(out, f"{sanitize(title)}.{real_ext}")

    print(f"[+] downloading -> {out}")
    with s.get(file_url, stream=True, timeout=60,
               headers={"Referer": page_url, "User-Agent": UA}) as dl:
        if dl.status_code != 200:
            sys.exit(f"file download failed: HTTP {dl.status_code}")
        total = int(dl.headers.get("Content-Length") or 0)
        done = 0
        with open(out, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1 << 16):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if not args.no_progress:
                    if total:
                        pct = done * 100 // total
                        print(f"\r    {done}/{total} bytes ({pct}%)", end="", flush=True)
                    else:
                        print(f"\r    {done} bytes", end="", flush=True)
    if not args.no_progress:
        print()
    print(f"[OK] saved {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    main()
