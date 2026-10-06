# docer-downloader-cli

Paste a docer or scribd link, get the file. No browser extension needed.

Inspired by [seszele64/docer-downloader](https://github.com/seszele64/docer-downloader)
and [Phoenix124/scribd-downloader](https://github.com/Phoenix124/scribd-downloader).

## Install

```bash
pip install git+https://github.com/naczo5/docer-downloader-cli
```

Or run without installing:

```bash
uvx --from git+https://github.com/naczo5/docer-downloader-cli docer-dl "<url>"
```

## Usage

```bash
docer-dl "https://docer.pl/doc/s00nxc5"
docer-dl "https://docer.pl/doc/s00nxc5" -o out.pdf
docer-dl "https://www.scribd.com/document/55949937/33-Strategies-of-War"
```

Works on docer.pl, docer.ar, docer.com.ar, docero.de, doceru.com and scribd.com
documents (text docs save as `.md`, image docs as `.pdf`). Scribd books and
audiobooks live on Everand behind a paid account and are not supported.

If docer throws a captcha, the tool opens the image and asks you to type the code.
