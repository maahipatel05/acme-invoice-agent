"""Generate a small real PDF fixture without dependencies; not a general PDF writer."""
from pathlib import Path


def demo_pdf():
    text = ['INVOICE', 'Vendor: Widgets Inc.', 'Invoice Number: ACME-PDF-001',
            'Due Date: 2026-12-01', 'Currency: USD',
            'WidgetA qty: 2 unit price: $250.00', 'Total: $500.00']
    stream = ('BT /F1 12 Tf 50 780 Td 20 TL ' + ' '.join(f'({t}) Tj T*' for t in text) + ' ET').encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    output = b'%PDF-1.4\n'; offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(output)); output += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(output)
    output += b'xref\n0 6\n0000000000 65535 f \n'
    output += b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
    return output + f'trailer\n<< /Root 1 0 R /Size 6 >>\nstartxref\n{start}\n%%EOF\n'.encode()


if __name__ == '__main__':
    target = Path(__file__).resolve().parents[1] / 'data/scenarios/clean.pdf'
    target.write_bytes(demo_pdf())
    print(target)
