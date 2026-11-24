.PHONY: demo backend-test backend-lint frontend-check

demo:
	./scripts/demo.sh

backend-test:
	cd backend && ../.venv/bin/pytest

backend-lint:
	cd backend && ../.venv/bin/ruff check app tests

frontend-check:
	cd frontend && npm run typecheck && npm run build && npm test
