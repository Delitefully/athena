PYTHON ?= /usr/bin/python3
GO ?= $(shell command -v go || echo /opt/homebrew/bin/go)

.PHONY: test
test:
	cd dash && $(GO) vet ./... && $(GO) test ./...
	$(PYTHON) -m unittest discover -s tests -t . -p 'test_*.py'
