#!/usr/bin/env python3
"""Export the leakage-safe MRO-RAG ATA benchmark to a two-column XLSX workbook."""
from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT = PROJECT_ROOT / "tests" / "fixtures" / "ata_impact_from_mro_rag.jsonl"
OUTPUT = PROJECT_ROOT / "tests" / "fixtures" / "ata_impact_mro_rag_readable.xlsx"


def cell(reference: str, value: str, style: int) -> str:
    element = ET.Element("c", {"r": reference, "t": "inlineStr", "s": str(style)})
    inline = ET.SubElement(element, "is")
    text = ET.SubElement(inline, "t")
    text.text = value
    return ET.tostring(element, encoding="unicode")


def main() -> None:
    rows: list[tuple[str, str]] = []
    for line in INPUT.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        data = row.get("input") or {}
        request = " ".join(str(data.get("description") or "").split())
        ata = "; ".join(f"ATA {str(item).replace(':', '-')}" for item in row.get("expected_ata") or [])
        if request and ata:
            rows.append((request, ata))

    worksheet_rows = [
        '<row r="1">' + cell("A1", "Запрос", 1) + cell("B1", "Список ATA", 1) + "</row>"
    ]
    for index, (request, ata) in enumerate(rows, start=2):
        worksheet_rows.append(f'<row r="{index}" ht="42" customHeight="1">' + cell(f"A{index}", request, 2) + cell(f"B{index}", ata, 2) + "</row>")

    worksheet = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">
  <sheetViews><sheetView workbookViewId=\"0\"><pane ySplit=\"1\" topLeftCell=\"A2\" activePane=\"bottomLeft\" state=\"frozen\"/></sheetView></sheetViews>
  <cols><col min=\"1\" max=\"1\" width=\"95\" customWidth=\"1\"/><col min=\"2\" max=\"2\" width=\"28\" customWidth=\"1\"/></cols>
  <sheetData>""" + "".join(worksheet_rows) + "</sheetData>" + f'<autoFilter ref="A1:B{len(rows) + 1}"/>' + "</worksheet>"
    styles = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<styleSheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><fonts count=\"2\"><font><sz val=\"11\"/><name val=\"Calibri\"/></font><font><b/><sz val=\"11\"/><name val=\"Calibri\"/></font></fonts><fills count=\"2\"><fill><patternFill patternType=\"none\"/></fill><fill><patternFill patternType=\"solid\"><fgColor rgb=\"FFD9EAF7\"/><bgColor indexed=\"64\"/></patternFill></fill></fills><borders count=\"1\"><border/></borders><cellStyleXfs count=\"1\"><xf/></cellStyleXfs><cellXfs count=\"3\"><xf xfId=\"0\"/><xf xfId=\"0\" fontId=\"1\" fillId=\"1\" applyFont=\"1\" applyFill=\"1\"/><xf xfId=\"0\" applyAlignment=\"1\"><alignment wrapText=\"1\" vertical=\"top\"/></xf></cellXfs></styleSheet>"""
    content_types = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"><Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/><Default Extension=\"xml\" ContentType=\"application/xml\"/><Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/><Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/><Override PartName=\"/xl/styles.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml\"/></Types>"""
    root_rels = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/></Relationships>"""
    workbook = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"><sheets><sheet name=\"MRO-RAG ATA\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>"""
    workbook_rels = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"><Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/><Relationship Id=\"rId2\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles\" Target=\"styles.xml\"/></Relationships>"""

    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", content_types)
        book.writestr("_rels/.rels", root_rels)
        book.writestr("xl/workbook.xml", workbook)
        book.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        book.writestr("xl/worksheets/sheet1.xml", worksheet)
        book.writestr("xl/styles.xml", styles)
    print(f"{OUTPUT}: {len(rows)} rows")


if __name__ == "__main__":
    main()
