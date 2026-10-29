# Evaluation: gemini mode

Run 2026-10-28 20:10 UTC with `gemini-3.5-flash-lite`.

- Outcomes correct: **35/35**
- Expected findings raised: **47/47**
- Extraction fields correct (official invoices): **139/139**
- Average time per invoice: 8.4s; 89 model calls, 64,751 tokens in total

## Supplied invoices (one shared ledger, filename order)

| File | Expected | Actual | Findings | Fields | Time |
|---|---|---|---|---|---|
| invoice_1001.txt | paid | paid | - | 6/6 | 4.0s |
| invoice_1002.txt | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED | 5/5 | 1.0s |
| invoice_1003.txt | rejected | rejected | OVER_APPROVAL_LIMIT, PAYMENT_PRESSURE, RELATIVE_DUE_DATE, UNKNOWN_VENDOR, ZERO_STOCK_ITEM | 5/5 | 1.0s |
| invoice_1004.json | paid | paid | - | 6/6 | 37.0s |
| invoice_1004_revised.json | duplicate | duplicate | DUPLICATE_PAID | 7/7 | 0.0s |
| invoice_1005.json | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED | 7/7 | 0.0s |
| invoice_1006.csv | paid | paid | - | 6/6 | 2.9s |
| invoice_1007.csv | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED, TOTAL_MISMATCH | 7/7 | 0.0s |
| invoice_1008.txt | rejected | rejected | UNKNOWN_ITEM, UNKNOWN_VENDOR | 6/6 | 1.4s |
| invoice_1009.json | rejected | rejected | INVALID_AMOUNT, INVALID_QUANTITY, MISSING_DUE_DATE, MISSING_VENDOR, SUBTOTAL_MISMATCH | 6/6 | 0.0s |
| invoice_1010.txt | pending_review | pending_review | PRICE_VARIANCE | 8/8 | 4.6s |
| invoice_1011.pdf | paid | paid | - | 6/6 | 4.8s |
| invoice_1011.txt | duplicate | duplicate | DUPLICATE_PAID | 6/6 | 1.2s |
| invoice_1012.pdf | pending_review | pending_review | EXTRACTION_UNCERTAINTY, VENDOR_RENAMED | 7/7 | 46.6s |
| invoice_1012.txt | pending_review | pending_review | DUPLICATE_PENDING, EXTRACTION_UNCERTAINTY, VENDOR_RENAMED | 7/7 | 3.9s |
| invoice_1013.json | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED, TOTAL_MISMATCH | 12/12 | 0.0s |
| invoice_1013.pdf | rejected | rejected | EXTRACTION_UNCERTAINTY, OVER_APPROVAL_LIMIT, STOCK_EXCEEDED, TOTAL_MISMATCH | 12/12 | 7.0s |
| invoice_1014.xml | pending_review | pending_review | NON_USD_CURRENCY | 6/6 | 3.5s |
| invoice_1015.csv | paid | paid | - | 7/7 | 3.0s |
| invoice_1016.json | rejected | rejected | UNKNOWN_ITEM | 7/7 | 0.0s |

## Added scenarios

| File | Expected | Actual | Findings | Fields | Time |
|---|---|---|---|---|---|
| bank_change.txt | pending_review | pending_review | PAYMENT_PRESSURE | - | 43.2s |
| clean.csv | paid | paid | - | - | 2.9s |
| clean.json | paid | paid | - | - | 2.7s |
| clean.pdf | paid | paid | - | - | 5.0s |
| clean.txt | paid | paid | - | - | 48.3s |
| credit_note.txt | rejected | rejected | INVALID_AMOUNT, INVALID_QUANTITY | - | 1.9s |
| foreign.xml | pending_review | pending_review | NON_USD_CURRENCY | - | 2.6s |
| fractional_qty.json | rejected | rejected | INVALID_QUANTITY | - | 0.0s |
| high_value.json | pending_review | pending_review | OVER_APPROVAL_LIMIT | - | 2.7s |
| homoglyph_vendor.txt | pending_review | pending_review | VENDOR_LOOKALIKE | - | 4.4s |
| lookalike_vendor.txt | pending_review | pending_review | VENDOR_LOOKALIKE | - | 49.2s |
| messy_email.txt | paid | paid | - | - | 3.8s |
| overbilled.json | pending_review | pending_review | PRICE_VARIANCE | - | 3.1s |
| prompt_injection.txt | rejected | rejected | PROMPT_INJECTION, UNKNOWN_VENDOR, ZERO_STOCK_ITEM | - | 1.2s |
| stock_mismatch.txt | rejected | rejected | OVER_APPROVAL_LIMIT, STOCK_EXCEEDED | - | 1.0s |

