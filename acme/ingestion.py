"""Read invoice bytes: native parsers for structured formats, text recovery for TXT/PDF."""
import csv
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from .domain import InputError
from .layouts import labelled_text, legacy_csv

MAX_BYTES = 5 * 1024 * 1024
EXTENSIONS = {'.txt', '.json', '.csv', '.xml', '.pdf'}


def _rows_from_words(page):
    """Rebuild visual rows from word boxes. PyMuPDF's plain text puts every table
    cell on its own line, which separates labels from values ("Vendor:\\nSummit")."""
    groups = {}
    for x0, y0, x1, y1, word, block, line, _ in page.get_text('words'):
        groups.setdefault((block, line), []).append((x0, y0, y1, word))
    rows = []
    for words in groups.values():
        words.sort()
        middle = sum((w[1] + w[2]) / 2 for w in words) / len(words)
        row = next((r for r in rows if abs(r['y'] - middle) < 3), None)
        if row is None:
            row = {'y': middle, 'cells': []}
            rows.append(row)
        row['cells'].append((words[0][0], ' '.join(w[3] for w in words)))
    return '\n'.join('  '.join(text for _, text in sorted(r['cells'])) for r in sorted(rows, key=lambda r: r['y']))


def pdf_text(data):
    try:
        import pymupdf
    except ImportError:
        if sys.platform == 'darwin':
            # Optional fallback for a Mac without PyMuPDF installed.
            try:
                helper = Path(__file__).resolve().parents[1] / 'tools/pdf_text.swift'
                output = subprocess.run(['swift', '-module-cache-path', '/private/tmp/acme-swift-cache', str(helper)],
                                        input=data, capture_output=True, timeout=45, check=True).stdout.decode('utf-8').strip()
            except (OSError, subprocess.SubprocessError):
                raise InputError('PDF support needs: pip install -r requirements.txt') from None
            if not output:
                raise InputError('Scanned PDF has no text layer; use --mode gemini to read it')
            return output
        raise InputError('PDF support needs: pip install -r requirements.txt') from None
    try:
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            if doc.is_encrypted or len(doc) > 30:
                raise InputError('Encrypted PDF or more than 30 pages')
            text = '\n'.join(_rows_from_words(page) for page in doc)
    except InputError:
        raise
    except Exception as exc:
        raise InputError('Unreadable PDF') from exc
    if not text.strip():
        raise InputError('Scanned PDF has no text layer; use --mode gemini to read it')
    return text


def _unique_json(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise InputError('Duplicate JSON keys are ambiguous')
        obj[k] = v
    return obj


def _xml(text):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise InputError('XML declarations/entities are unsupported')
    root = ET.fromstring(text)

    def fields(elements):
        values = {}
        for element in elements:
            if element.tag in values:
                raise InputError('Repeated XML field is ambiguous; correct the source')
            values[element.tag] = element.text
        return values
    raw = fields(root.findall('./header/*') + root.findall('./totals/*'))
    raw['items'] = [fields(item) for item in root.findall('./line_items/item')]
    return raw


def read_document(filename, data, allow_scanned=False):
    """Return {'format', 'raw', 'text'}.

    raw: deterministic field extraction (None when the layout is not recognized).
    text: the document's text, kept for model extraction, grounding and fraud-signal scans.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in EXTENSIONS or not data or len(data) > MAX_BYTES:
        raise InputError('Upload a nonempty TXT, JSON, CSV, XML or PDF up to 5 MB')
    if suffix == '.pdf':
        if not data.startswith(b'%PDF-'):
            raise InputError('Invalid PDF signature')
        try:
            text = pdf_text(data)
        except InputError:
            if not allow_scanned:
                raise
            return {'format': 'pdf', 'raw': None, 'text': None}
        return {'format': 'pdf', 'raw': labelled_text(text), 'text': text}
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError as exc:
        raise InputError('Text must be UTF-8') from exc
    if len(text) > 200000:
        raise InputError('Text exceeds 200,000 character limit')
    try:
        if suffix == '.json':
            return {'format': 'json', 'raw': json.loads(text, object_pairs_hook=_unique_json), 'text': text}
        if suffix == '.xml':
            return {'format': 'xml', 'raw': _xml(text), 'text': text}
        if suffix == '.csv':
            return {'format': 'csv', 'raw': legacy_csv(text), 'text': text}
        # Ambiguity checks (repeated headers, unparsed money) stay authoritative even
        # when a model will also read this text.
        return {'format': 'txt', 'raw': labelled_text(text), 'text': text}
    except (json.JSONDecodeError, ET.ParseError, csv.Error, RecursionError) as exc:
        raise InputError('Malformed structured document; correct the source rather than guessing') from exc
