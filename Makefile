.PHONY: restore static test api browser smoke verify web

restore:
	python -c "import fastapi, uvicorn, httpx, playwright"

static:
	PYTHONPATH=src python -m compileall -q src tests
	node --check src/student_execution_os/web/static/app.js

test:
	PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v

api:
	PYTHONPATH=src python -m unittest tests.web.web_api -v

browser:
	PYTHONPATH=src python -m unittest tests.browser.browser_ui -v

smoke:
	PYTHONPATH=src python -m student_execution_os health
	PYTHONPATH=src python -m student_execution_os domain-smoke
	PYTHONPATH=src python -m student_execution_os feasibility-smoke
	PYTHONPATH=src python -m student_execution_os planner-smoke
	PYTHONPATH=src python -m student_execution_os reconciliation-smoke
	PYTHONPATH=src python -m student_execution_os connector-smoke
	PYTHONPATH=src python -m student_execution_os agent-smoke
	PYTHONPATH=src python -m student_execution_os travel-smoke
	PYTHONPATH=src python -m student_execution_os recurrence-notification-smoke

web:
	PYTHONPATH=src python -m student_execution_os.web.server --help >/dev/null

verify: restore static test api browser smoke web
