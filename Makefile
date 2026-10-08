PYTHON ?= /usr/bin/python3

.PHONY: test
test:
	$(PYTHON) -m unittest discover -s tests -t . -p 'test_*.py'
