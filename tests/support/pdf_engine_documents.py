"""Owned, deterministic text/tree and image-only-fact visual acceptance PDFs."""
import io


def documents():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    def pdf(pages):
        writer = PdfWriter()
        font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")}))
        for title, body, drawing in pages:
            page = writer.add_blank_page(width=612, height=792)
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
                DictionaryObject({NameObject("/F1"): font})})
            stream = DecodedStreamObject()
            text = f"BT /F1 22 Tf 50 730 Td ({title}) Tj /F1 16 Tf 0 -45 Td ({body}) Tj ET\n"
            stream.set_data((text + drawing).encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
        result = io.BytesIO()
        writer.write(result)
        return result.getvalue()

    tree = pdf([
        ("Orion Programme", "The intake semester is Autumn.", ""),
        ("Rowan Biography", "Dr Rowan joined in 2011.", ""),
        ("Vega Workshop", "The workshop carries 3 credits.", ""),
        ("Lyra Office", "The office is located in Block C.", ""),
        ("Atlas Permit", "The permit must be renewed after 60 days.", ""),
        ("Comet Award", "The minimum qualifying score is 72.", ""),
    ])
    visual = pdf([
        ("Panel One", "Rectangle sample", "q 0 0 1 rg 160 280 290 250 re f Q"),
        ("Regional Output", "", "q 0.25 0.45 0.8 rg 130 200 80 140 re f 270 200 80 300 re f "
         "410 200 80 210 re f Q BT /F1 16 Tf 130 170 Td (North) Tj 140 0 Td (South) Tj 140 0 Td (East) Tj ET"),
        ("Product Status", "", "q 0 0.75 0 rg 100 400 400 60 re f Q "
         "0 0 0 RG 100 340 400 180 re S 100 400 m 500 400 l S 100 460 m 500 460 l S "
         "BT /F1 18 Tf 120 480 Td (Product) Tj 0 -60 Td (Arctic) Tj 0 -60 Td (Boreal) Tj ET"),
        ("Orbit Diagram", "", "q 0.6 0.2 0.7 rg " + " ".join(
            f"{x+35} 400 m {x+35} 419 {x+19} 435 {x} 435 c {x-19} 435 {x-35} 419 {x-35} 400 c "
            f"{x-35} 381 {x-19} 365 {x} 365 c {x+19} 365 {x+35} 381 {x+35} 400 c f"
            for x in (160, 300, 440)) + " Q"),
        ("Annual Trend", "", "q 0 0 0 RG 2 w 120 200 m 120 550 l S 120 200 m 500 200 l S "
         "0 0.3 0.8 RG 5 w 180 280 m 440 480 l S Q "
         "BT /F1 18 Tf 155 165 Td (2024) Tj 260 0 Td (2025) Tj ET"),
        ("Containment Figure", "Outer Frame", "q 0 0 0 RG 3 w 110 250 390 320 re S "
         "0.8 0.2 0.1 rg 300 510 m 190 310 l 410 310 l h f Q"),
    ])
    return {"tree.pdf": tree, "visual.pdf": visual}
