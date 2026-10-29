# Evaluation: offline mode

Run 2026-10-28 20:23 UTC.

- Outcomes correct: **35/35**
- Expected findings raised: **47/47**
- Extraction fields correct (official invoices): **139/139**
- Average time per invoice: 0.0s

## Supplied invoices (one shared ledger, filename order)

| File | Expected | Actual | Findings | Fields | Time |
|---|---|---|---|---|---|
| invoice_1001.txt | paid | paid | - | 6/6 | 0.0s |
| invoice_1002.txt | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED | 5/5 | 0.0s |
| invoice_1003.txt | rejected | rejected | OVER_APPROVAL_LIMIT, PAYMENT_PRESSURE, RELATIVE_DUE_DATE, UNKNOWN_VENDOR, ZERO_STOCK_ITEM | 5/5 | 0.0s |
| invoice_1004.json | paid | paid | - | 6/6 | 0.0s |
| invoice_1004_revised.json | duplicate | duplicate | DUPLICATE_PAID | 7/7 | 0.0s |
| invoice_1005.json | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED | 7/7 | 0.0s |
| invoice_1006.csv | paid | paid | - | 6/6 | 0.0s |
| invoice_1007.csv | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED, TOTAL_MISMATCH | 7/7 | 0.0s |
| invoice_1008.txt | rejected | rejected | UNKNOWN_ITEM, UNKNOWN_VENDOR | 6/6 | 0.0s |
| invoice_1009.json | rejected | rejected | INVALID_AMOUNT, INVALID_QUANTITY, MISSING_DUE_DATE, MISSING_VENDOR, SUBTOTAL_MISMATCH | 6/6 | 0.0s |
| invoice_1010.txt | pending_review | pending_review | PRICE_VARIANCE | 8/8 | 0.0s |
| invoice_1011.pdf | paid | paid | - | 6/6 | 0.2s |
| invoice_1011.txt | duplicate | duplicate | DUPLICATE_PAID | 6/6 | 0.0s |
| invoice_1012.pdf | pending_review | pending_review | EXTRACTION_UNCERTAINTY, VENDOR_RENAMED | 7/7 | 0.2s |
| invoice_1012.txt | pending_review | pending_review | DUPLICATE_PENDING, EXTRACTION_UNCERTAINTY, VENDOR_RENAMED | 7/7 | 0.0s |
| invoice_1013.json | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED, TOTAL_MISMATCH | 12/12 | 0.0s |
| invoice_1013.pdf | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED, TOTAL_MISMATCH | 12/12 | 0.2s |
| invoice_1014.xml | pending_review | pending_review | NON_USD_CURRENCY | 6/6 | 0.0s |
| invoice_1015.csv | paid | paid | - | 7/7 | 0.0s |
| invoice_1016.json | rejected | rejected | UNKNOWN_ITEM | 7/7 | 0.0s |

## Added scenarios

| File | Expected | Actual | Findings | Fields | Time |
|---|---|---|---|---|---|
| bank_change.txt | pending_review | pending_review | PAYMENT_PRESSURE | - | 0.0s |
| clean.csv | paid | paid | - | - | 0.0s |
| clean.json | paid | paid | - | - | 0.0s |
| clean.pdf | paid | paid | - | - | 0.2s |
| clean.txt | paid | paid | - | - | 0.0s |
| credit_note.txt | rejected | rejected | INVALID_AMOUNT, INVALID_QUANTITY | - | 0.0s |
| foreign.xml | pending_review | pending_review | NON_USD_CURRENCY | - | 0.0s |
| fractional_qty.json | rejected | rejected | INVALID_QUANTITY | - | 0.0s |
| high_value.json | pending_review | pending_review | OVER_APPROVAL_LIMIT | - | 0.0s |
| homoglyph_vendor.txt | pending_review | pending_review | VENDOR_LOOKALIKE | - | 0.0s |
| lookalike_vendor.txt | pending_review | pending_review | VENDOR_LOOKALIKE | - | 0.0s |
| messy_email.txt | rejected | rejected | UNREADABLE_DOCUMENT | - | 0.0s |
| overbilled.json | pending_review | pending_review | PRICE_VARIANCE | - | 0.0s |
| prompt_injection.txt | rejected | rejected | PROMPT_INJECTION, UNKNOWN_VENDOR, ZERO_STOCK_ITEM | - | 0.0s |
| stock_mismatch.txt | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED | - | 0.0s |

