"""Build the extra evaluation documents that need no OCR: digital (text-layer) national IDs.

TEMPORARY TEST DATA, fictional people from data/build_mock_registry.py.

- national_id_person2.pdf        the second fictional person's ID (case A8: someone else's ID)
- national_id_arabic_digits.pdf  the first person's ID with Arabic-Indic digits (case V5, format control)
- national_id_name_changed.pdf   a forged ID: the first person's national number with another name (case A10)
- graduation_notice_foreign_qr.png  the graduation notice with its QR code replaced by one that points to a
                                 look-alike website (same token, foreign address) (case A9)

They are plain PDFs whose text layer is exactly what the page shows, rendered with a
font that has Arabic glyphs (auto-detected like the notice PDFs; set NOTICE_FONT_PATH
otherwise). Unlike the scanned-looking attack PDFs, they need no Tesseract to build.

Usage:  python evaluation/build_extra_documents.py            (builds the files that are missing)
        python evaluation/build_extra_documents.py --force    (rebuilds all of them)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import get_settings  # noqa: E402

OUT = ROOT / "data" / "eval_documents"

ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

PERSON1 = [("الاسم", "نور"), ("النسبة", "السيد"), ("اسم الأب", "فايز"), ("اسم الأم", "رغداء الزعبي"),
           ("مكان الولادة", "اللاذقية"), ("تاريخ الولادة", "1996/07/20"), ("الرقم الوطني", "14749200009")]
PERSON2 = [("الاسم", "ليلى"), ("النسبة", "حداد"), ("اسم الأب", "سامر"), ("اسم الأم", "هالة نجار"),
           ("مكان الولادة", "حمص"), ("تاريخ الولادة", "1997/03/09"), ("الرقم الوطني", "11223344556")]
# Forged: person 1's number and birth data, but a different name (nobody with this name is in the registry).
FORGED = [("الاسم", "سامر"), ("النسبة", "الخطيب"), ("اسم الأب", "خالد"), ("اسم الأم", "منى العلي"),
          ("مكان الولادة", "اللاذقية"), ("تاريخ الولادة", "1996/07/20"), ("الرقم الوطني", "14749200009")]
FOREIGN_QR_PREFIX = "https://verify.damascus-uni.example/verify/"  # look-alike of the registry, reserved TLD


def id_pdf(out: Path, fields: list[tuple[str, str]], arabic_digits: bool = False) -> None:
    font = get_settings().notice_font_path
    if font is None or not font.exists():
        raise SystemExit("No font with Arabic glyphs found; set NOTICE_FONT_PATH in .env")
    rows = "".join(
        f"<tr><td><b>{k}:</b></td><td>{v.translate(ARABIC_DIGITS) if arabic_digits else v}</td></tr>" for k, v in fields
    )
    html = f"""
    <div dir="rtl">
    <h2 style="text-align:center">الجمهورية العربية السورية</h2>
    <h3 style="text-align:center">بطاقة شخصية</h3>
    <table>{rows}</table>
    <p style="color:#b00"><b>نموذج تجريبي - بيانات وهمية / TEST SPECIMEN - FICTITIOUS DATA</b></p>
    </div>"""
    css = f"@font-face {{font-family: f; src: url({font.name});}} * {{font-family: f; font-size: 14px;}}"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=380)
    page.insert_htmlbox(pymupdf.Rect(30, 20, 570, 360), html, css=css, archive=pymupdf.Archive(str(font.parent)))
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.subset_fonts()
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    print(f"wrote {out.relative_to(ROOT)}")


def foreign_qr_notice(out: Path) -> None:
    """The graduation notice, its QR code covered by a new one: same token, foreign website."""
    import cv2
    import numpy as np

    from config import QR_VERIFY_PREFIX

    src = ROOT / "data" / "documents" / "graduation_notice_test.webp"
    img = cv2.cvtColor(np.array(__import__("PIL.Image", fromlist=["Image"]).open(src).convert("RGB")), cv2.COLOR_RGB2BGR)
    from agent.tools import _decode_qr_png

    ok, buf = cv2.imencode(".png", img)
    original = _decode_qr_png(buf.tobytes())
    if not original or not original[0].startswith(QR_VERIFY_PREFIX):
        raise SystemExit("could not read the original QR code")
    token = original[0][len(QR_VERIFY_PREFIX):]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    pts = None
    for scale in (1, 2, 3):  # small, blurred QR codes are only found enlarged and binarized (as in read_qr)
        im = gray if scale == 1 else cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        for v in (im, cv2.threshold(im, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
            found, p = cv2.QRCodeDetector().detect(v)
            if found and p is not None:
                pts = p[0] / scale
                break
        if pts is not None:
            break
    if pts is None:
        raise SystemExit("could not locate the original QR code")
    x0, y0 = pts.min(axis=0).astype(int)
    x1, y1 = pts.max(axis=0).astype(int)
    pad = 6
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    qr = cv2.QRCodeEncoder.create().encode(FOREIGN_QR_PREFIX + token)
    qr = cv2.resize(qr, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    img[y0:y1, x0:x1] = cv2.cvtColor(qr, cv2.COLOR_GRAY2BGR)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    ok, buf = cv2.imencode(".png", img)
    assert _decode_qr_png(buf.tobytes()) == [FOREIGN_QR_PREFIX + token], "the new QR code does not decode"
    print(f"wrote {out.relative_to(ROOT)}")


BUILD = {
    "national_id_person2.pdf": lambda o: id_pdf(o, PERSON2),
    "national_id_arabic_digits.pdf": lambda o: id_pdf(o, PERSON1, arabic_digits=True),
    "national_id_name_changed.pdf": lambda o: id_pdf(o, FORGED),
    "graduation_notice_foreign_qr.png": foreign_qr_notice,
}


def main() -> None:
    force = "--force" in sys.argv
    for name, build in BUILD.items():
        if force or not (OUT / name).exists():
            build(OUT / name)


if __name__ == "__main__":
    main()
