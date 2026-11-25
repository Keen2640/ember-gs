.PHONY: install test lint demo passes db

install:
	pip install -e ".[dev,aws]"

test:
	pytest -q

lint:
	ruff check src tests
	cfn-lint infra/*.yaml

# Full link-layer demo on a synthetic, damaged downlink
demo:
	ember synth /tmp/ember-demo.bin --packets 1000 --shift 5 --invert --ber 5e-4
	ember decode /tmp/ember-demo.bin

passes:
	ember passes --sat NOAA-20 --hours 24 --min-max-el 30

db:
	docker compose up -d db
