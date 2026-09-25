from __future__ import annotations

import io
from pathlib import Path

import fitz


SOURCE = Path(r"C:\Users\juanc\Downloads\DIM CON LEVANTE.pdf")
OUTPUT = Path(r"C:\Users\juanc\Downloads\impromarkas_dev\output\pdf\032026001312834-9_censurado.pdf")
TERMS = (
    "ARGELIA INTERNACIONAL S.A.",
    "LIBRE COLON",
    "EDIF HAYATUR NO. 1. AVE SANTA ISABEL ENTRE CALLE 1",
    "507 441 5543",
    "4.781.00",
    "12.79",
    "56.54",
    "4.837.54",
)


def main() -> None:
    source = fitz.open(SOURCE)
    output = fitz.open()
    output.insert_pdf(source, from_page=2, to_page=2)
    source.close()
    redactions = 0
    page = output[0]
    for term in TERMS:
        rectangles = page.search_for(term)
        if not rectangles:
            raise RuntimeError(f"Sensitive value not found: {term}")
        for rectangle in rectangles:
            page.add_redact_annot(rectangle, fill=(0, 0, 0), cross_out=False)
            redactions += 1
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)
    output.set_metadata({})
    if output.xref_xml_metadata():
        output.del_xml_metadata()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    output.save(OUTPUT, garbage=4, deflate=True, clean=True)
    output.close()
    print(f"Created {OUTPUT} with {redactions} applied redactions")


if __name__ == "__main__":
    main()
