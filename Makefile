PY ?= python3

.PHONY: install test eval eval-live batch serve docker

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m unittest discover -b -s tests

eval:
	$(PY) evals/run.py

eval-live:
	$(PY) evals/run.py --mode gemini

batch:
	$(PY) main.py --batch data/invoices --quiet

serve:
	$(PY) main.py --serve

docker:
	docker build -t acme-invoices . && docker run --rm -p 8080:8080 acme-invoices
