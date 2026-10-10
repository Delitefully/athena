PYTHON ?= /usr/bin/python3
GO ?= $(shell command -v go || echo /opt/homebrew/bin/go)
CLAUDE ?= $(shell command -v claude)

.PHONY: test
test:
	cd dash && $(GO) vet ./... && $(GO) test ./...
	$(PYTHON) -m unittest discover -s tests -t . -p 'test_*.py'
	@if [ -n "$(CLAUDE)" ]; then $(CLAUDE) plugin validate . && $(CLAUDE) plugin test .; \
	else echo "claude not found: the athena-watch mod's checks are skipped"; fi
