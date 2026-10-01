"""Builds the sample documents in examples/documents/ (all companies and numbers are fictional).

    pip install weasyprint pillow && python examples/make_examples.py

1. fattura_tipografia_alpina.pdf   invoice, PDF with a text layer (readable without AI), everything correct
2. ddt_ortofrutta.jpg              delivery note, phone photo (needs AI or manual entry), no prices
3. scontrino_bar_centrale.jpg      till receipt, phone photo, prices include VAT
4. fattura_idraulica_scan.pdf      scanned invoice (PDF without text), VAT printed wrong: 30,00 instead of 47,30
5. fattura_tipografia_foto.jpg     photo of invoice 1 printed out: a different file, the same document (duplicate)
"""

from __future__ import annotations

import io
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
from weasyprint import HTML

OUT = Path(__file__).parent / "documents"
CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: "DejaVu Sans", sans-serif; font-size: 10pt; color: #111; }
.head { display: flex; justify-content: space-between; border-bottom: 2px solid #333; padding-bottom: 8px; }
.brand { font-size: 16pt; font-weight: bold; }
h1 { font-size: 14pt; margin: 18px 0 6px; }
.parties { display: flex; gap: 40px; margin: 10px 0 14px; }
.parties div { flex: 1; border: 1px solid #999; padding: 8px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; }
th, td { border: 1px solid #999; padding: 5px 6px; } th { background: #eee; text-align: left; }
td.n { text-align: right; }
.tot { width: 45%; margin-left: auto; margin-top: 12px; } .tot td.b { font-weight: bold; }
.small { font-size: 8.5pt; color: #444; margin-top: 18px; }
"""


def invoice_html(sup: dict, cust: dict, title: str, meta: list[tuple[str, str]], lines: list[tuple], totals:
                 list[tuple[str, str]], foot: str = "", prices: bool = True) -> str:
    price_cells = "<th>Prezzo</th><th>IVA %</th><th>Importo</th>" if prices else "<th>U.M.</th>"
    head_cells = "<th>Descrizione</th><th>Q.tà</th>" + price_cells
    rows = "".join("<tr>" + "".join(f'<td class="{"n" if i else ""}">{v}</td>' for i, v in enumerate(r)) + "</tr>"
                   for r in lines)
    tot = "".join(f'<tr><td>{k}</td><td class="n {"b" if "Totale" in k else ""}">{v}</td></tr>' for k, v in totals)
    meta_html = " · ".join(f"<b>{k}</b> {v}" for k, v in meta)
    return f"""<html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="head"><div><div class="brand">{sup['name']}</div>{sup['addr']}<br>P.IVA {sup['vat']}</div>
<div style="text-align:right">{sup.get('extra', '')}</div></div>
<h1>{title}</h1><div>{meta_html}</div>
<div class="parties"><div><b>Cedente / Mittente</b><br>{sup['name']}<br>P.IVA {sup['vat']}</div>
<div><b>Cessionario / Destinatario</b><br>{cust['name']}<br>{cust['addr']}<br>P.IVA {cust['vat']}</div></div>
<table><tr>{head_cells}</tr>{rows}</table>
{f'<table class="tot">{tot}</table>' if totals else ''}
<div class="small">{foot}</div></body></html>"""


def receipt_html() -> str:
    return """<html><head><meta charset="utf-8"><style>
@page { size: 80mm 150mm; margin: 6mm; } body { font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; }
.c { text-align: center; } table { width: 100%; } td.n { text-align: right; }
hr { border: 0; border-top: 1px dashed #000; }
</style></head><body>
<div class="c"><b>BAR CENTRALE SRL</b><br>Piazza Walther 3 - 39100 Bolzano<br>P.IVA 01633904576</div>
<div class="c">DOCUMENTO COMMERCIALE<br>di vendita o prestazione</div><hr>
<table><tr><td>DESCRIZIONE</td><td class="n">IVA</td><td class="n">Prezzo(€)</td></tr>
<tr><td>2 x CAFFE' ESPRESSO</td><td class="n">10%</td><td class="n">2,40</td></tr>
<tr><td>1 x CORNETTO</td><td class="n">10%</td><td class="n">1,50</td></tr>
<tr><td>1 x SPREMUTA ARANCIA</td><td class="n">10%</td><td class="n">3,50</td></tr></table><hr>
<table><tr><td><b>TOTALE COMPLESSIVO</b></td><td class="n"><b>7,40</b></td></tr>
<tr><td>di cui IVA</td><td class="n">0,67</td></tr>
<tr><td>Pagamento elettronico</td><td class="n">7,40</td></tr></table><hr>
<div class="c">20-09-2026 08:42<br>DOC.N. 0042-0117<br>RT 99MEX012345</div></body></html>"""


def pdf(html: str) -> bytes:
    return HTML(string=html).write_pdf()


def render_page(pdf_bytes: bytes, dpi: int = 150) -> Image.Image:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return doc[0].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        doc.close()


def phone_photo(img: Image.Image, angle: float, seed: int) -> bytes:
    """Paper-coloured, slightly rotated, a bit soft and noisy: like a photo taken on a desk."""
    rnd = random.Random(seed)
    paper = Image.new("RGB", img.size, (247, 242, 228))
    img = Image.blend(img, paper, 0.12)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(92, 84, 76))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = ImageEnhance.Brightness(img).enhance(0.97)
    draw = ImageDraw.Draw(img)
    for _ in range(img.width * img.height // 900):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        g = rnd.randrange(-18, 18)
        r, gg, b = img.getpixel((x, y))
        draw.point((x, y), fill=(max(0, min(255, r + g)), max(0, min(255, gg + g)), max(0, min(255, b + g))))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82)
    return out.getvalue()


def image_only_pdf(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PDF", resolution=150)
    return out.getvalue()


CUSTOMER = {"name": "Ristorante Laurin Srl", "addr": "Via Museo 12 - 39100 Bolzano (BZ)", "vat": "IT03971560424"}
TIPOGRAFIA = {"name": "Tipografia Alpina Srl", "addr": "Via Galilei 8 - 39100 Bolzano (BZ)", "vat": "IT02849173055",
              "extra": "info@tipografia-alpina.example<br>IBAN IT60 X054 2811 1010 0000 0123 456"}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    invoice = pdf(invoice_html(
        TIPOGRAFIA, CUSTOMER, "FATTURA",
        [("Numero", "118/2026"), ("Data", "15/09/2026"), ("Scadenza", "15/10/2026"), ("Pagamento", "Bonifico")],
        [("Menu plastificati A4", "40", "3,50", "22", "140,00"),
         ("Volantini A5 fronte/retro", "2000", "0,045", "22", "90,00"),
         ("Progetto grafico", "1", "60,00", "22", "60,00")],
        [("Imponibile", "290,00"), ("IVA 22%", "63,80"), ("Totale documento €", "353,80")],
        "Fattura elettronica trasmessa allo SDI. Copia di cortesia."))
    (OUT / "fattura_tipografia_alpina.pdf").write_bytes(invoice)

    ddt = pdf(invoice_html(
        {"name": "Ortofrutta Bolzano Srl", "addr": "Via Macello 31 - 39100 Bolzano (BZ)", "vat": "IT04516288307"},
        CUSTOMER, "DOCUMENTO DI TRASPORTO (DDT)",
        [("Numero", "2026/0457"), ("Data", "18/09/2026"), ("Causale", "Vendita"), ("Trasporto", "Mittente")],
        [("Pomodori ramati", "12", "kg"), ("Insalata gentile", "20", "pz"), ("Limoni Amalfi", "6", "kg"),
         ("Basilico fresco", "10", "mazzi")],
        [], "Firma del conducente: ____________   Firma del destinatario: ____________", prices=False))
    (OUT / "ddt_ortofrutta.jpg").write_bytes(phone_photo(render_page(ddt, 120), -1.6, 1))

    receipt = render_page(pdf(receipt_html()), 200)
    (OUT / "scontrino_bar_centrale.jpg").write_bytes(phone_photo(receipt, 2.2, 2))

    plumber = pdf(invoice_html(
        {"name": "Idraulica Rossi Snc", "addr": "Via Druso 140 - 39100 Bolzano (BZ)", "vat": "IT02188744607"},
        CUSTOMER, "FATTURA", [("Numero", "37"), ("Data", "22/09/2026"), ("Scadenza", "22/10/2026")],
        [("Riparazione caldaia cucina", "1", "180,00", "22", "180,00"),
         ("Ricambio valvola di sicurezza", "1", "35,00", "22", "35,00")],
        [("Imponibile", "215,00"), ("IVA 22%", "30,00"), ("Totale documento €", "245,00")]))
    scan = render_page(plumber, 150).convert("L").convert("RGB")
    (OUT / "fattura_idraulica_scan.pdf").write_bytes(image_only_pdf(scan))

    (OUT / "fattura_tipografia_foto.jpg").write_bytes(phone_photo(render_page(invoice, 120), 1.1, 3))
    for path in sorted(OUT.iterdir()):
        print(f"{path.name:34} {path.stat().st_size // 1024:>5} KB")


if __name__ == "__main__":
    main()
