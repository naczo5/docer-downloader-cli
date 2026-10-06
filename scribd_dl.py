#!/usr/bin/env python3
"""Scribd document download (technique from Phoenix124/scribd-downloader).

Free documents only, no account. Text docs save as .md, image docs as .pdf.
Needs nothing beyond stdlib + requests.
"""
import html
import os
import re
import shutil
import sys
import tempfile
import urllib.parse

try:
    import requests
except ImportError:
    sys.exit("missing dependency: pip install -r requirements.txt")

TIMEOUT = 30

JSONP_RE = re.compile(r'https?://[^\s"\'\\]+\.jsonp')
CALLBACK_RE = re.compile(r'^\s*window\.page\d+_callback\(\["')
ORIG_RE = re.compile(r'orig=\\?"(https?://[^"\\]+)')
ABSIMG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
SPAN_RE = re.compile(r'<span\b[^>]*\bclass="a"[^>]*>(.*?)</span>',
                     re.DOTALL | re.IGNORECASE)
H1_RE = re.compile(r'<h1\b[^>]*>(.*?)</h1>', re.DOTALL | re.IGNORECASE)


class ScribdError(Exception):
    pass


def _session(referer=None):
    # NOTE: no browser User-Agent here on purpose. Scribd's bot check
    # (Fastly) challenges requests whose UA claims to be a browser but
    # whose TLS handshake isn't one (python-requests can't fake that),
    # while plain requests with the default UA pass through fine.
    # Upstream scribd-downloader does the same: bare requests.get().
    s = requests.Session()
    if referer:
        s.headers["Referer"] = referer
    return s


def is_challenge(page_html: str) -> bool:
    return "<title>Client Challenge</title>" in page_html


def extract_title(page_html: str) -> str:
    m = H1_RE.search(page_html)
    if not m:
        raise ScribdError("could not find the title on the page (layout changed?)")
    title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    title = re.sub(r"^Currently Reading:\s*", "", title)
    if not title:
        raise ScribdError("could not find the title on the page (layout changed?)")
    return title


def sanitize(name: str) -> str:
    name = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", name)[:120] or "document"


def jsonp_urls(page_html: str):
    """All .jsonp page-data URLs, deduped, order kept."""
    seen, out = set(), []
    for u in JSONP_RE.findall(page_html):
        u = html.unescape(u)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def text_spans(fragment: str):
    """Text of every page span, tags stripped, entities unescaped."""
    out = []
    for m in SPAN_RE.finditer(fragment):
        t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if t:
            out.append(t)
    return out


def unwrap_jsonp(payload: str) -> str:
    payload = CALLBACK_RE.sub("", payload, count=1)
    payload = payload.replace("\\n", "").replace("\\", "")
    if payload.endswith('"]);'):
        payload = payload[:-4]
    return payload


def absimg_urls(page_html: str):
    """Page-image URLs from <img class="... absimg ..."> tags."""
    out = []
    for tag in ABSIMG_RE.findall(page_html):
        if "absimg" not in tag:
            continue
        m = re.search(r'\borig="([^"]+)"', tag) or re.search(r'\bsrc="([^"]+)"', tag)
        if m:
            out.append(_secure(html.unescape(m.group(1))))
    return out


def jsonp_image_urls(payload: str, jsonp_url: str):
    urls = [_secure(u) for u in ORIG_RE.findall(payload)]
    if not urls:  # fall back to guessing, same as upstream
        urls = [jsonp_url.replace("/pages/", "/images/").replace(".jsonp", ".jpg")]
    return urls


def _secure(url: str) -> str:
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def _get_bytes(session, url: str, what: str, no_progress: bool) -> bytes:
    with session.get(url, stream=True, timeout=TIMEOUT) as r:
        if r.status_code != 200:
            raise ScribdError(f"{what} failed: HTTP {r.status_code} ({url})")
        total = int(r.headers.get("Content-Length") or 0)
        done, chunks = 0, []
        for chunk in r.iter_content(chunk_size=1 << 16):
            if not chunk:
                continue
            chunks.append(chunk)
            done += len(chunk)
            if not no_progress:
                if total:
                    print(f"\r    {what}: {done}/{total} bytes ({done * 100 // total}%)",
                          end="", flush=True)
                else:
                    print(f"\r    {what}: {done} bytes", end="", flush=True)
    if not no_progress:
        print()
    return b"".join(chunks)


def jpeg_info(data: bytes):
    """(width, height, components) of a JPEG, or None. Pure stdlib."""
    try:
        if data[:2] != b"\xff\xd8":
            return None
        i, n = 2, len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                i += 1
                continue
            m = data[i + 1]
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7 or m == 0x01:
                i += 2
                continue
            seg = (data[i + 2] << 8) + data[i + 3]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                     0xC9, 0xCB, 0xCD, 0xCE, 0xCF):
                h = (data[i + 5] << 8) + data[i + 6]
                w = (data[i + 7] << 8) + data[i + 8]
                return w, h, data[i + 9]
            i += 2 + seg
    except IndexError:
        pass
    return None


def write_jpeg_pdf(images, out_path: str):
    """Combine JPEGs into a PDF (embedded as-is via DCTDecode). No deps."""
    # layout: 1 catalog, 2 pages, then per image: page, image, content
    n = len(images)
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [%s] /Count %d >>"
         % (" ".join(f"{3 + 3 * i} 0 R" for i in range(n)), n)).encode(),
    ]
    for i, (data, w, h, gray) in enumerate(images):
        p, im, co = 3 + 3 * i, 4 + 3 * i, 5 + 3 * i
        cs = b"/DeviceGray" if gray else b"/DeviceRGB"
        bodies.append(
            ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
             "/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
             % (w, h, im, co)).encode())
        bodies.append(
            (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
             b"/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\n"
             b"stream\n" % (w, h, cs, len(data)) + data + b"\nendstream"))
        bodies.append(
            ("q %d 0 0 %d 0 0 cm /Im0 Do Q" % (w, h)).encode())
    out, offsets = [b"%PDF-1.4\n"], []
    for idx, body in enumerate(bodies, start=1):
        offsets.append(sum(len(x) for x in out))
        out.append(b"%d 0 obj\n" % idx + body + b"\nendobj\n")
    xref = sum(len(x) for x in out)
    out.append(b"xref\n0 %d\n" % (len(bodies) + 1))
    out.append(b"0000000000 65535 f \n")
    for off in offsets:
        out.append(b"%010d 00000 n \n" % off)
    out.append(("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
                % (len(bodies) + 1, xref)).encode())
    with open(out_path, "wb") as f:
        f.write(b"".join(out))


def _combine_images(files, out_path: str):
    """Build a PDF from downloaded page images. Returns True on success."""
    images = []
    for path in files:
        with open(path, "rb") as f:
            data = f.read()
        info = jpeg_info(data)
        if info is None:
            return False
        w, h, comps = info
        images.append((data, w, h, comps == 1))
    write_jpeg_pdf(images, out_path)
    return True


def _resolve_output(output, default_name: str) -> str:
    if not output:
        return default_name
    if os.path.isdir(output):
        return os.path.join(output, default_name)
    return output


def download_scribd(url: str, output=None, no_progress: bool = False) -> str:
    """Download a free Scribd document. Returns the saved file path."""
    url = url.strip().strip("'\"")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    if "everand.com" in url:
        raise ScribdError("books and audiobooks moved to Everand (paid account needed) — "
                          "only free scribd.com documents are supported")

    s = _session()
    print(f"[+] fetching page: {url}")
    try:
        r = s.get(url, timeout=TIMEOUT)
    except Exception as e:
        raise ScribdError(f"page fetch failed: {e}")
    if r.status_code != 200:
        raise ScribdError(f"page fetch failed: HTTP {r.status_code}")
    if is_challenge(r.text):
        raise ScribdError("scribd showed a browser check (\"Client Challenge\"). "
                          "Wait a few minutes and retry — firing many requests "
                          "in a row gets flagged")
    if "everand.com" in r.url:
        raise ScribdError("scribd redirected to Everand — only free documents are supported")

    page, page_url = r.text, r.url
    title = sanitize(extract_title(page))
    s = _session(referer=page_url)
    urls = jsonp_urls(page)

    # text or images? (upstream asks the user; we auto-detect)
    if absimg_urls(page):
        image_mode = True
    elif text_spans(page):
        image_mode = False
    elif urls:
        probe = _get_bytes(s, urls[0], "probing", True).decode("utf-8", "replace")
        image_mode = bool(ORIG_RE.search(unwrap_jsonp(probe)))
    else:
        raise ScribdError("no readable content found on the page (layout changed?)")
    print(f"[+] document: {title} ({'images' if image_mode else 'text'})")

    if not image_mode:
        paras = text_spans(page)
        for u in urls:
            raw = s.get(u, timeout=TIMEOUT).text
            paras.extend(text_spans(unwrap_jsonp(raw)))
        if not paras:
            raise ScribdError("no text found (font-scrambled docs need image mode)")
        out = _resolve_output(output, f"{title}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n" + "\n\n".join(paras) + "\n")
        print(f"[OK] saved {out} ({os.path.getsize(out)} bytes)")
        return out

    img_urls, seen = [], set()
    for u in absimg_urls(page):
        if u not in seen:
            seen.add(u)
            img_urls.append(u)
    for u in urls:
        raw = s.get(u, timeout=TIMEOUT).text
        for iu in jsonp_image_urls(unwrap_jsonp(raw), u):
            if iu not in seen:
                seen.add(iu)
                img_urls.append(iu)
    if not img_urls:
        raise ScribdError("no page images found (layout changed?)")

    tmp = tempfile.mkdtemp(prefix="scribd-")
    files = []
    try:
        for i, u in enumerate(img_urls, 1):
            ext = os.path.splitext(urllib.parse.urlparse(u).path)[1] or ".jpg"
            path = os.path.join(tmp, f"page_{i:03d}{ext}")
            print(f"[+] page {i}/{len(img_urls)}")
            with open(path, "wb") as f:
                f.write(_get_bytes(s, u, f"page {i}", no_progress))
            files.append(path)
        out = _resolve_output(output, f"{title}.pdf")
        if os.path.splitext(out)[1].lower() in (".md", ".txt"):
            print(f"[!] note: {out} will contain PDF data (image document)")
        if _combine_images(files, out):
            print(f"[OK] saved {out} ({os.path.getsize(out)} bytes)")
            return out
        keep = f"{os.path.splitext(out)[0]}_pages"
        os.makedirs(keep, exist_ok=True)
        for path in files:
            shutil.move(path, os.path.join(keep, os.path.basename(path)))
        raise ScribdError(f"images are not plain JPEGs, left them in ./{keep}/ — "
                          "convert with: img2pdf *.jpg -o out.pdf")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Download free scribd.com documents")
    p.add_argument("url", help="scribd document URL")
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--no-progress", action="store_true")
    a = p.parse_args()
    try:
        download_scribd(a.url, output=a.output, no_progress=a.no_progress)
    except ScribdError as e:
        sys.exit(f"[-] {e}")
