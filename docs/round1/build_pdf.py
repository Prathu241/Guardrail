"""Build the Round 1 PDF from ABSTRACT.md and architecture.svg.

    python docs/round1/build_pdf.py

Writes abstract.html next to this script and, if Playwright is installed (pip install playwright;
playwright install chromium), prints it to Guardrail_Round1_Abstract.pdf. Without Playwright,
open abstract.html in a browser and use Print > Save as PDF (A4, margins default, background graphics on).
"""

import html
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSS = """
@page { size: A4; margin: 16mm 17mm; }
body { font-family: "IBM Plex Sans", Arial, sans-serif; font-size: 10pt; line-height: 1.42; color: #1b2230; margin: 0; }
h1 { font-family: "IBM Plex Sans Condensed", Arial, sans-serif; font-size: 17pt; margin: 0 0 3pt; }
.meta { color: #5a6475; font-size: 9pt; margin: 0 0 6pt; padding-bottom: 6pt; border-bottom: 1px solid #d8dde6; }
h2 { font-size: 10.5pt; color: #2c4a8a; margin: 9pt 0 2pt; text-transform: uppercase; letter-spacing: 0.04em; }
p, li { margin: 0 0 4pt; text-align: justify; }
ol { margin: 2pt 0 4pt 16pt; padding: 0; }
.diagram { break-before: page; }
.diagram svg { box-sizing: border-box; width: 100%; height: auto; display: block; margin-top: 8pt;
               border: 1px solid #d8dde6; border-radius: 6px; padding: 6pt; }
.caption { color: #5a6475; font-size: 9pt; margin-top: 8pt; }
"""
CAPTION = ("The dashed blue box is the only place anything changes: everything in it happens in one database "
           "transaction, so a check cannot pass on stale records and a crash leaves nothing half-written. Payments "
           "and messages cannot be rolled back, so the tool records them as outbox intents and the dispatcher delivers "
           "them after commit with the intent's id as idempotency key. The reconciler compares the providers' records "
           "with the outbox and audit log.")


def inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(text, quote=False))


def render(markdown: str) -> str:
    out, in_list = [], False
    for line in markdown.splitlines():
        if in_list and not re.match(r"\d+\. ", line):
            out.append("</ol>")
            in_list = False
        if line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif re.match(r"\d+\. ", line):
            if not in_list:
                out.append("<ol>")
                in_list = True
            out.append(f"<li>{inline(line.split('. ', 1)[1])}</li>")
        elif line.strip():
            css_class = ' class="meta"' if line.startswith("**Team**") else ""
            out.append(f"<p{css_class}>{inline(line)}</p>")
    if in_list:
        out.append("</ol>")
    return "\n".join(out)


def main() -> None:
    body = render((HERE / "ABSTRACT.md").read_text())
    svg = (HERE / "architecture.svg").read_text()
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Guardrail Round 1 Abstract</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600&display=swap">
<style>{CSS}</style></head><body>
{body}
<section class="diagram"><h2>Architecture</h2>
<p>How one proposed tool call moves through Guardrail. Outcomes are colour-coded and labelled: allow (green),
escalate (amber), block (red).</p>
{svg}
<p class="caption">{CAPTION}</p></section>
</body></html>"""
    out_html = HERE / "abstract.html"
    out_html.write_text(page)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"wrote {out_html}; open it in a browser and Print > Save as PDF")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch()
        tab = browser.new_page()
        tab.goto(out_html.as_uri())
        tab.wait_for_timeout(1500)  # web fonts
        tab.pdf(path=str(HERE / "Guardrail_Round1_Abstract.pdf"), format="A4", print_background=True,
                prefer_css_page_size=True)
        browser.close()
    print(f"wrote {HERE / 'Guardrail_Round1_Abstract.pdf'}")


if __name__ == "__main__":
    main()
