#!/usr/bin/env python3
"""Build the deployable site from the canonical page.

`index.html` (repo root) is the canonical page *content* — it deliberately
has no <!doctype>/<head>/<body>, because the Claude artifact platform wraps
it at publish time. For real hosting (GitHub Pages at zinscheck.ch) this
script wraps the same content into a complete standalone document and
writes it to docs/index.html, plus the CNAME file GitHub Pages needs.

Run from the repo root:  python3 scripts/build_site.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "index.html"
OUT_DIR = ROOT / "docs"
DOMAIN = "zinscheck.ch"

# Red rounded square with a white % — deliberately not a Swiss cross
# (commercial use of the federal cross is restricted, Wappenschutzgesetz).
FAVICON = (
    "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
    "viewBox='0 0 64 64'><rect width='64' height='64' rx='14' "
    "fill='%23E23324'/><text x='32' y='45' font-size='36' font-weight='700' "
    "font-family='-apple-system,Helvetica,Arial' text-anchor='middle' "
    "fill='white'>%25</text></svg>"
)

HEAD = f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zinscheck — Mietzins senken beim aktuellen Referenzzinssatz</title>
<meta name="description" content="Referenzzinssatz aktuell: Prüfen Sie in 10 Sekunden, ob Sie Anspruch auf eine Mietzinssenkung haben — mit Briefvorlage für das Senkungsbegehren. Für die Schweiz, in DE/FR/IT/EN.">
<link rel="canonical" href="https://{DOMAIN}/">
<meta property="og:title" content="Zinscheck — Mietzins-Check Schweiz">
<meta property="og:description" content="Referenzzinssatz gesunken? Berechnen Sie Ihr Senkungspotenzial und erhalten Sie die passende Briefvorlage.">
<meta property="og:url" content="https://{DOMAIN}/">
<meta property="og:type" content="website">
<meta name="theme-color" content="#F5F5F7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0A0A0C" media="(prefers-color-scheme: dark)">
<link rel="icon" href="{FAVICON}">
</head>
<body>
"""

# Cookieless analytics — only on the hosted site, never in the artifact preview.
FOOT = (
    '<script data-goatcounter="https://zinscheck.goatcounter.com/count" '
    'async src="https://gc.zgo.at/count.js"></script>\n'
    "</body>\n</html>\n"
)


def main() -> None:
    content = SRC.read_text(encoding="utf-8")
    # Strip the leading <title> line — the artifact platform reads it, but the
    # standalone document carries its own richer <title> in HEAD.
    first_line, _, rest = content.partition("\n")
    if first_line.strip().startswith("<title>"):
        content = rest
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "index.html").write_text(HEAD + content + FOOT, encoding="utf-8")
    (OUT_DIR / "CNAME").write_text(DOMAIN + "\n", encoding="utf-8")
    print(f"built docs/index.html ({(OUT_DIR / 'index.html').stat().st_size:,} bytes) + docs/CNAME")


if __name__ == "__main__":
    main()
