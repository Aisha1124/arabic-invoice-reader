"""Generate ZATCA-format Arabic/English invoice images with exact ground truth.

Output:
    samples/INV-0001.png ...   invoice images
    ground_truth.json          exact field values for every sample

No network access required. All amounts are arithmetically consistent unless the
sample is deliberately seeded with a defect (see DEFECTS).
"""

from __future__ import annotations

import base64
import json
import random
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageFilter

SEED = 20260916
OUT = Path("out")
SAMPLES = OUT / "samples"
VAT_RATE = Decimal("0.15")

FONT_DIR = Path("fonts")
AR_FONTS = [FONT_DIR / "Amiri-Regular.ttf", FONT_DIR / "NotoNaskhArabic-Regular.ttf"]
AR_BOLD = FONT_DIR / "Amiri-Bold.ttf"
LATIN = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
LATIN_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

SELLERS = [
    ("شركة الرياض للتجارة المحدودة", "Riyadh Trading Company Ltd"),
    ("مؤسسة النخيل للمقاولات", "Al Nakheel Contracting Est"),
    ("شركة البحر الأحمر للخدمات اللوجستية", "Red Sea Logistics Co"),
    ("مؤسسة الفيصلية للتوريدات", "Al Faisaliah Supplies Est"),
    ("شركة جدة للأغذية", "Jeddah Foods Company"),
    ("مكتبة الحكمة للقرطاسية", "Al Hikma Stationery"),
    ("شركة الخليج للمعدات الطبية", "Gulf Medical Equipment Co"),
    ("مؤسسة الدمام لقطع الغيار", "Dammam Spare Parts Est"),
    ("شركة الواحة لتقنية المعلومات", "Al Waha Information Technology"),
    ("مطعم بيت الشاورما", "Shawarma House Restaurant"),
]

BUYERS = [
    ("شركة المستقبل القابضة", "Future Holding Company"),
    ("مؤسسة الأمانة التجارية", "Al Amanah Trading Est"),
    ("شركة نماء للاستثمار", "Namaa Investment Company"),
    ("مجموعة الصفوة", "Al Safwa Group"),
    ("شركة التقنية الحديثة", "Modern Technology Company"),
]

CITIES = [
    ("الرياض", "Riyadh"), ("جدة", "Jeddah"), ("الدمام", "Dammam"),
    ("مكة المكرمة", "Makkah"), ("المدينة المنورة", "Madinah"), ("الخبر", "Khobar"),
]

ITEMS = [
    ("ورق تصوير A4 - علبة", "A4 Copy Paper - Box", 45, 95),
    ("حبر طابعة ليزر", "Laser Printer Toner", 180, 420),
    ("خدمة صيانة شهرية", "Monthly Maintenance Service", 500, 2500),
    ("كرسي مكتبي دوار", "Office Swivel Chair", 320, 890),
    ("جهاز حاسب محمول", "Laptop Computer", 2400, 6500),
    ("كابل شبكة - 5 متر", "Network Cable - 5m", 12, 38),
    ("خدمة نقل وشحن", "Transport and Shipping", 150, 800),
    ("مياه معدنية - كرتون", "Mineral Water - Carton", 18, 32),
    ("قهوة عربية - كيلو", "Arabic Coffee - 1kg", 65, 140),
    ("شاشة عرض 27 بوصة", "27-inch Monitor", 780, 1900),
    ("استشارة هندسية", "Engineering Consultancy", 1200, 4500),
    ("تركيب وتشغيل", "Installation and Commissioning", 400, 1500),
    ("سندويتش شاورما دجاج", "Chicken Shawarma Sandwich", 12, 22),
    ("عصير طازج", "Fresh Juice", 8, 18),
]

UNITS_AR = ["حبة", "علبة", "خدمة", "كرتون", "ساعة"]

# Deliberate defects seeded into specific samples so the validator has real work.
# index -> defect name
DEFECTS = {
    3: "missing_buyer_vat",      # standard invoice with no buyer TIN (top ZATCA rejection)
    9: "lumped_vat",             # single VAT figure, no per-line breakdown (2nd most common)
    14: "missing_buyer_vat",     # rolls simplified under SEED 20260916, so no defect is applied
    21: "lumped_vat",
    26: "missing_seller_vat",
}

ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Line:
    description_ar: str
    description_en: str
    quantity: str
    unit_price: str
    line_total: str
    vat_rate: str
    vat_amount: str


@dataclass
class Invoice:
    invoice_number: str
    invoice_date: str
    invoice_timestamp: str
    invoice_type: str
    seller_name: str
    seller_name_en: str
    seller_vat_number: str | None
    seller_city: str
    buyer_name: str | None
    buyer_name_en: str | None
    buyer_vat_number: str | None
    currency: str
    line_items: list[Line]
    subtotal: str
    vat_total: str
    total: str
    qr_base64: str | None
    seeded_defect: str | None
    numerals: str
    language: str


def ksa_vat_number(rng: random.Random) -> str:
    """15-digit KSA TIN: starts with 3, ends with 3."""
    middle = "".join(str(rng.randint(0, 9)) for _ in range(13))
    return "3" + middle + "3"


def tlv(tag: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return bytes([tag, len(raw)]) + raw


def zatca_qr(seller: str, vat: str, ts: str, total: str, vat_amt: str) -> str:
    """ZATCA simplified-invoice QR: TLV tags 1-5, base64 encoded."""
    payload = (
        tlv(1, seller) + tlv(2, vat) + tlv(3, ts) + tlv(4, total) + tlv(5, vat_amt)
    )
    return base64.b64encode(payload).decode("ascii")


def build_invoice(idx: int, rng: random.Random) -> Invoice:
    defect = DEFECTS.get(idx)
    simplified = rng.random() < 0.45
    # A simplified invoice has no buyer, so a missing buyer VAT is correct there,
    # not a defect. This was a real bug: the original set shipped with
    # INV-2026-1014 (index 14, rolled simplified) labelled missing_buyer_vat.
    # The check consumes no randomness, so the rendered invoices are unchanged.
    if defect == "missing_buyer_vat" and simplified:
        defect = None

    seller_ar, seller_en = rng.choice(SELLERS)
    city_ar, city_en = rng.choice(CITIES)
    seller_vat = None if defect == "missing_seller_vat" else ksa_vat_number(rng)

    if simplified:
        buyer_ar = buyer_en = buyer_vat = None
    else:
        buyer_ar, buyer_en = rng.choice(BUYERS)
        buyer_vat = None if defect == "missing_buyer_vat" else ksa_vat_number(rng)

    date = datetime(2026, 1, 1) + timedelta(
        days=rng.randint(0, 250), hours=rng.randint(8, 20), minutes=rng.randint(0, 59)
    )

    n_lines = rng.randint(1, 3) if simplified else rng.randint(2, 6)
    lines: list[Line] = []
    subtotal = Decimal("0")
    vat_total = Decimal("0")

    for _ in range(n_lines):
        ar_desc, en_desc, lo, hi = rng.choice(ITEMS)
        qty = Decimal(rng.randint(1, 12))
        unit = money(rng.uniform(lo, hi))
        line_total = money(qty * unit)
        vat_amt = money(line_total * VAT_RATE)
        subtotal += line_total
        vat_total += vat_amt
        lines.append(
            Line(
                description_ar=ar_desc,
                description_en=en_desc,
                quantity=str(qty),
                unit_price=str(unit),
                line_total=str(line_total),
                vat_rate=str(VAT_RATE),
                vat_amount="" if defect == "lumped_vat" else str(vat_amt),
            )
        )

    subtotal = money(subtotal)
    vat_total = money(vat_total)
    total = money(subtotal + vat_total)
    ts = date.strftime("%Y-%m-%dT%H:%M:%SZ")

    qr = None
    if simplified and seller_vat:
        qr = zatca_qr(seller_ar, seller_vat, ts, str(total), str(vat_total))

    numerals = "arabic_indic" if rng.random() < 0.25 else "latin"
    language = "bilingual" if rng.random() < 0.6 else "arabic_only"

    return Invoice(
        invoice_number=f"INV-2026-{1000 + idx}",
        invoice_date=date.strftime("%Y-%m-%d"),
        invoice_timestamp=ts,
        invoice_type="simplified" if simplified else "standard",
        seller_name=seller_ar,
        seller_name_en=seller_en,
        seller_vat_number=seller_vat,
        seller_city=city_ar,
        buyer_name=buyer_ar,
        buyer_name_en=buyer_en,
        buyer_vat_number=buyer_vat,
        currency="SAR",
        line_items=lines,
        subtotal=str(subtotal),
        vat_total=str(vat_total),
        total=str(total),
        qr_base64=qr,
        seeded_defect=defect,
        numerals=numerals,
        language=language,
    )


class Canvas:
    """Thin drawing helper. Arabic is passed raw; raqm handles shaping and bidi."""

    def __init__(self, w: int, h: int):
        self.img = Image.new("RGB", (w, h), "white")
        self.d = ImageDraw.Draw(self.img)
        self.w = w
        self.h = h

    def ar(self, xy, text, size, bold=False, fill="black", anchor="ra", font_path=None):
        path = font_path or (AR_BOLD if bold else AR_FONTS[0])
        f = ImageFont.truetype(str(path), size)
        self.d.text(xy, text, font=f, fill=fill, anchor=anchor,
                    direction="rtl", language="ar")

    def en(self, xy, text, size, bold=False, fill="black", anchor="la"):
        f = ImageFont.truetype(str(LATIN_BOLD if bold else LATIN), size)
        self.d.text(xy, text, font=f, fill=fill, anchor=anchor)

    def line(self, y, x0=None, x1=None, fill=(170, 170, 170), width=1):
        self.d.line([(x0 or 40, y), (x1 or self.w - 40, y)], fill=fill, width=width)


def num(s: str, style: str) -> str:
    return s.translate(ARABIC_INDIC) if style == "arabic_indic" else s


def render(inv: Invoice, rng: random.Random, ar_font: Path) -> Image.Image:
    W = 1240
    H = 720 + len(inv.line_items) * 46
    c = Canvas(W, H)
    R = W - 60          # right margin (Arabic baseline)
    L = 60              # left margin (English)
    bilingual = inv.language == "bilingual"
    y = 48

    # Header
    c.ar((R, y), inv.seller_name, 34, bold=True, font_path=ar_font)
    if bilingual:
        c.en((L, y + 6), inv.seller_name_en, 20, bold=True)
    y += 48
    c.ar((R, y), inv.seller_city, 22, font_path=ar_font)
    y += 38

    title_ar = "فاتورة ضريبية مبسطة" if inv.invoice_type == "simplified" else "فاتورة ضريبية"
    title_en = "Simplified Tax Invoice" if inv.invoice_type == "simplified" else "Tax Invoice"
    c.line(y)
    y += 18
    c.ar((R, y), title_ar, 30, bold=True, font_path=ar_font)
    if bilingual:
        c.en((L, y + 6), title_en, 19, bold=True)
    y += 52
    c.line(y)
    y += 20

    # Seller / buyer block
    if inv.seller_vat_number:
        c.ar((R, y), f"الرقم الضريبي للبائع: {num(inv.seller_vat_number, inv.numerals)}",
             21, font_path=ar_font)
        if bilingual:
            c.en((L, y + 3), f"Seller VAT: {inv.seller_vat_number}", 16)
        y += 34

    c.ar((R, y), f"رقم الفاتورة: {num(inv.invoice_number, inv.numerals)}", 21, font_path=ar_font)
    if bilingual:
        c.en((L, y + 3), f"Invoice No: {inv.invoice_number}", 16)
    y += 34

    c.ar((R, y), f"التاريخ: {num(inv.invoice_date, inv.numerals)}", 21, font_path=ar_font)
    if bilingual:
        c.en((L, y + 3), f"Date: {inv.invoice_date}", 16)
    y += 34

    if inv.buyer_name:
        c.ar((R, y), f"العميل: {inv.buyer_name}", 21, font_path=ar_font)
        if bilingual:
            c.en((L, y + 3), f"Buyer: {inv.buyer_name_en}", 16)
        y += 34
        if inv.buyer_vat_number:
            c.ar((R, y), f"الرقم الضريبي للعميل: {num(inv.buyer_vat_number, inv.numerals)}",
                 21, font_path=ar_font)
            if bilingual:
                c.en((L, y + 3), f"Buyer VAT: {inv.buyer_vat_number}", 16)
            y += 34

    y += 12
    c.line(y, width=2)
    y += 16

    # Table header
    lumped = inv.seeded_defect == "lumped_vat"
    cols_ar = ["الوصف", "الكمية", "السعر", "الإجمالي"] + ([] if lumped else ["الضريبة"])
    xs = [R, R - 440, R - 590, R - 760] + ([] if lumped else [R - 930])
    for cx, label in zip(xs, cols_ar):
        c.ar((cx, y), label, 20, bold=True, font_path=ar_font)
    y += 34
    c.line(y)
    y += 10

    for ln in inv.line_items:
        c.ar((xs[0], y), ln.description_ar, 19, font_path=ar_font)
        c.ar((xs[1], y), num(ln.quantity, inv.numerals), 19, font_path=ar_font)
        c.ar((xs[2], y), num(ln.unit_price, inv.numerals), 19, font_path=ar_font)
        c.ar((xs[3], y), num(ln.line_total, inv.numerals), 19, font_path=ar_font)
        if not lumped:
            c.ar((xs[4], y), num(ln.vat_amount, inv.numerals), 19, font_path=ar_font)
        y += 44

    c.line(y, width=2)
    y += 22

    # Totals
    for label, value in [
        ("الإجمالي قبل الضريبة", inv.subtotal),
        ("ضريبة القيمة المضافة 15%", inv.vat_total),
        ("الإجمالي شامل الضريبة", inv.total),
    ]:
        bold = label.startswith("الإجمالي شامل")
        c.ar((R, y), label, 22, bold=bold, font_path=ar_font)
        c.ar((R - 620, y), f"{num(value, inv.numerals)} {inv.currency}", 22,
             bold=bold, font_path=ar_font)
        y += 40

    # QR for simplified invoices
    if inv.qr_base64:
        q = qrcode.QRCode(box_size=4, border=1)
        q.add_data(inv.qr_base64)
        q.make(fit=True)
        qimg = q.make_image(fill_color="black", back_color="white").convert("RGB")
        qimg = qimg.resize((150, 150))
        c.img.paste(qimg, (L, min(y + 10, H - 170)))

    img = c.img

    # Scan-like degradation so the set is not trivially clean
    style = rng.random()
    if style < 0.30:
        img = img.rotate(rng.uniform(-1.2, 1.2), expand=True, fillcolor="white")
    if style < 0.45:
        img = img.filter(ImageFilter.GaussianBlur(0.6))
    if rng.random() < 0.35:
        img = img.convert("L").point(lambda p: min(255, int(p * rng.uniform(0.92, 1.0) + 12))).convert("RGB")

    return img


def main() -> None:
    rng = random.Random(SEED)
    SAMPLES.mkdir(parents=True, exist_ok=True)

    truth = []
    for i in range(30):
        inv = build_invoice(i, rng)
        ar_font = AR_FONTS[i % len(AR_FONTS)]
        img = render(inv, rng, ar_font)
        name = f"{inv.invoice_number}.png"
        img.save(SAMPLES / name, "PNG")
        record = asdict(inv)
        record["file"] = name
        truth.append(record)

    (OUT / "ground_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    types = {}
    defects = {}
    for r in truth:
        types[r["invoice_type"]] = types.get(r["invoice_type"], 0) + 1
        if r["seeded_defect"]:
            defects[r["seeded_defect"]] = defects.get(r["seeded_defect"], 0) + 1

    print(f"generated {len(truth)} invoices -> {SAMPLES}")
    print("types:", types)
    print("seeded defects:", defects)
    print("arabic-indic numerals:", sum(1 for r in truth if r["numerals"] == "arabic_indic"))
    print("arabic-only layout:", sum(1 for r in truth if r["language"] == "arabic_only"))
    print("with ZATCA QR:", sum(1 for r in truth if r["qr_base64"]))


if __name__ == "__main__":
    main()
