.PHONY: restore static test smoke verify

restore:
	@echo "No third-party runtime dependencies."

static:
	PYTHONPATH=src python -m compileall -q src tests

test:
	PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v

smoke:
	PYTHONPATH=src python -m student_execution_os health
	PYTHONPATH=src python -m student_execution_os domain-smoke
	PYTHONPATH=src python -m student_execution_os feasibility-smoke

verify: restore static test smoke
