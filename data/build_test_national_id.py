"""Generate data/documents/national_id_test.png: a FICTIONAL test national ID card.

TEMPORARY TEST DATA. The fields come from the fictional person in
data/build_mock_registry.py (same person as the non-employment test
document). It is a plain text layout, not a copy of the real card's design,
and it is marked "نموذج تجريبي - بيانات وهمية" / "TEST SPECIMEN - FICTITIOUS
DATA", like the other test documents.

Usage:  python data/build_test_national_id.py   (overwrites the file)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

OUT = Path(__file__).resolve().parent / "documents" / "national_id_test.png"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

FIELDS = [
    ("الاسم", "نور"),
    ("النسبة", "السيد"),
    ("اسم الأب", "فايز"),
    ("اسم الأم", "رغداء الزعبي"),
    ("مكان الولادة", "اللاذقية"),
    ("تاريخ الولادة", "1996/07/20"),
    ("الرقم الوطني", "14749200009"),
]


def main() -> None:
    if not features.check("raqm"):
        raise SystemExit("Pillow lacks libraqm: Arabic would render unshaped")
    W, H = 1000, 630
    img = Image.new("RGB", (W, H), (236, 241, 236))
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(FONT_BOLD, 34)
    f_sub = ImageFont.truetype(FONT_BOLD, 28)
    f_label = ImageFont.truetype(FONT_BOLD, 26)
    f_val = ImageFont.truetype(FONT, 26)
    f_foot = ImageFont.truetype(FONT_BOLD, 22)

    d.rounded_rectangle((8, 8, W - 8, H - 8), radius=28, outline=(60, 90, 70), width=4)
    d.text((W - 40, 30), "الجمهورية العربية السورية", font=f_title, fill=(20, 50, 30), anchor="ra", direction="rtl")
    d.text((W - 40, 78), "بطاقة شخصية", font=f_sub, fill=(20, 50, 30), anchor="ra", direction="rtl")

    # photo placeholder
    d.rectangle((50, 140, 270, 420), outline=(90, 90, 90), width=3, fill=(215, 220, 215))
    d.text((160, 280), "صورة", font=f_sub, fill=(120, 120, 120), anchor="mm", direction="rtl")

    y = 140
    for label, value in FIELDS:
        d.text((W - 40, y), label + ":", font=f_label, fill=(20, 20, 20), anchor="ra", direction="rtl")
        d.text((W - 260, y), value, font=f_val, fill=(20, 20, 20), anchor="ra", direction="rtl")
        y += 44

    # test-data markings
    d.rectangle((8, H - 92, W - 8, H - 8), fill=(190, 30, 30))
    d.text((W // 2, H - 66), "نموذج تجريبي - بيانات وهمية", font=f_foot, fill="white", anchor="mm", direction="rtl")
    d.text((W // 2, H - 34), "TEST SPECIMEN - FICTITIOUS DATA", font=f_foot, fill="white", anchor="mm")
    wm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(wm).text((W // 2, H // 2 - 20), "SPECIMEN", font=ImageFont.truetype(FONT_BOLD, 120),
                            fill=(190, 30, 30, 45), anchor="mm")
    img = Image.alpha_composite(img.convert("RGBA"), wm.rotate(18, center=(W // 2, H // 2))).convert("RGB")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
