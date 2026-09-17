PYTHON ?= python
DATASET ?= ../Track1/data/Market-cloudbed-1
QUERIES ?= $(DATASET)/query.csv
OUT ?= out/validate

.PHONY: test validate build
test:
	$(PYTHON) -B -m unittest discover -s tests -v
	$(PYTHON) -B -m unittest discover -s eval -p test_full_suite.py -v

# Shape checks are explicitly offline. The judged three-argument CLI defaults to routed.
validate:
	$(PYTHON) -B eval/validate.py --dataset "$(DATASET)" --queries "$(QUERIES)" --out "$(OUT)"

build:
	docker build -t track1-mini-rca .
