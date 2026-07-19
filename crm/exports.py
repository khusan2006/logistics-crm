"""Excel (.xlsx) export helpers built on openpyxl."""
from io import BytesIO

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.styles import Font

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_response(filename, headers, rows):
    """Build a one-sheet Workbook (bold header row + data rows) and return it as an
    HttpResponse download. `rows` is an iterable of iterables aligned with `headers`;
    money values should be raw Decimals/numbers, not formatted strings."""
    wb = Workbook()
    ws = wb.active
    ws.append(list(headers))
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(list(row))

    buffer = BytesIO()
    wb.save(buffer)

    response = HttpResponse(buffer.getvalue(), content_type=XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
