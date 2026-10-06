# docer-downloader-cli

Paste a docer link, get the file. No browser extension needed.

Inspired by [seszele64/docer-downloader](https://github.com/seszele64/docer-downloader).

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
```

Works on docer.pl, docer.ar, docer.com.ar, docero.de, doceru.com.

If docer throws a captcha, the tool opens the image and asks you to type the code.
