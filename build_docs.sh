#!/usr/bin/env bash
# Regenerate README.pdf and guide.pdf from the current markdown sources.
#
# Usage:
#   ./build_docs.sh           # rebuild both
#   ./build_docs.sh README    # rebuild only README.pdf
#   ./build_docs.sh guide     # rebuild only guide.pdf
#
# Requirements (installed once):
#   - python3 with the `markdown` package  (pip3 install --user markdown)
#   - google-chrome on PATH (used in headless mode for the HTML → PDF step)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
TARGETS=("$@")
[ "${#TARGETS[@]}" -eq 0 ] && TARGETS=(README guide)

build_one() {
	local base="$1"
	local md="$ROOT/$base.md"
	local pdf="$ROOT/$base.pdf"
	local html="/tmp/${base}.html"

	if [ ! -f "$md" ]; then
		echo "skip: $md not found"
		return
	fi

	python3 - "$md" "$html" "$base" <<'PY'
import sys
import pathlib
import markdown

md_path, html_path, base = sys.argv[1], sys.argv[2], sys.argv[3]
md_text = pathlib.Path(md_path).read_text(encoding="utf-8")
body = markdown.markdown(
	md_text,
	extensions=["extra", "sane_lists", "codehilite", "admonition"],
	extension_configs={
		"codehilite": {"guess_lang": False, "noclasses": True, "pygments_style": "friendly"},
	},
	output_format="html5",
)

css = r"""
:root {
	--fg: #1f2328;
	--bg: #ffffff;
	--border: #d1d9e0;
	--border-soft: #e4e8ec;
	--code-bg: #f6f8fa;
	--accent: #0969da;
	--note-bg: #ddf4ff;
	--note-border: #54aeff;
}
@page { size: A4; margin: 18mm 16mm 22mm 16mm; }
* { box-sizing: border-box; }
html, body {
	font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, "Noto Sans", sans-serif;
	font-size: 11pt;
	line-height: 1.55;
	color: var(--fg);
	background: var(--bg);
	margin: 0;
	padding: 0;
}
.wrap { max-width: 860px; margin: 0 auto; padding: 0 4mm; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin-top: 1.4em; margin-bottom: 0.5em; font-weight: 600; }
h1 { font-size: 2em; border-bottom: 1px solid var(--border); padding-bottom: 0.3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid var(--border); padding-bottom: 0.3em; page-break-after: avoid; }
h3 { font-size: 1.2em; page-break-after: avoid; }
h4 { font-size: 1.05em; page-break-after: avoid; }
p { margin: 0.6em 0; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
img[src*="img.shields.io"] { display: inline-block; height: 20px; margin: 4px 4px 4px 0; vertical-align: middle; }
ul, ol { padding-left: 1.8em; margin: 0.6em 0; }
li + li { margin-top: 0.25em; }
blockquote {
	margin: 0.8em 0;
	padding: 0.5em 1em;
	border-left: 4px solid var(--note-border);
	background: var(--note-bg);
	color: var(--fg);
}
blockquote p { margin: 0.3em 0; }
table {
	width: 100%;
	border-collapse: collapse;
	margin: 0.8em 0;
	font-size: 10.5pt;
	page-break-inside: avoid;
}
th, td {
	border: 1px solid var(--border-soft);
	padding: 6px 10px;
	text-align: left;
	vertical-align: top;
}
thead th { background: var(--code-bg); font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
pre, code, kbd, samp { font-family: "SFMono-Regular", "Menlo", "Consolas", monospace; font-size: 9.8pt; }
code { background: var(--code-bg); padding: 0.15em 0.4em; border-radius: 4px; }
pre {
	background: var(--code-bg);
	padding: 12px 14px;
	border-radius: 6px;
	overflow-x: auto;
	page-break-inside: avoid;
	border: 1px solid var(--border-soft);
}
pre code { background: transparent; padding: 0; border-radius: 0; }
hr { border: 0; border-top: 1px solid var(--border); margin: 1.5em 0; }
@media print {
	a { color: var(--fg); text-decoration: none; }
	tr, pre, blockquote, table { page-break-inside: avoid; }
	h1, h2, h3 { page-break-after: avoid; }
}
"""

title = f"pinelabs_fin — {'User Guide' if base.lower() == 'readme' else 'Guide'}"
html = (
	"<!doctype html><html lang=\"en\"><head>"
	"<meta charset=\"utf-8\" />"
	f"<title>{title}</title>"
	f"<style>{css}</style>"
	"</head><body><div class=\"wrap\">" + body + "</div></body></html>"
)
pathlib.Path(html_path).write_text(html, encoding="utf-8")
print(f"  HTML: {html_path} ({len(html):,} bytes)")
PY

	echo "  → rendering $pdf"
	google-chrome --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
		--print-to-pdf="$pdf" "file://$html" 2>/dev/null
	echo "  ✓ $(basename "$pdf") ($(du -h "$pdf" | cut -f1))"
}

for target in "${TARGETS[@]}"; do
	echo "[$target]"
	build_one "$target"
done
