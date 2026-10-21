SHELL := /bin/bash
.PHONY: check test demo probe-absolute

check:
	bash -n bin/releasecraft examples/demo.sh
	python3 -c "import ast,pathlib; [ast.parse(p.read_text()) for folder in ('lib','tests','examples') for p in pathlib.Path(folder).glob('*.py')]"
	$(MAKE) test

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

demo:
	bash examples/demo.sh

probe-absolute:
	python3 examples/probe_absolute.py
