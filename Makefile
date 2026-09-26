.PHONY: restore static test api browser smoke verify web mobile-sync apk

restore:
	python -c "import fastapi, uvicorn, httpx, playwright"

static:
	PYTHONPATH=src python -m compileall -q src tests
	find src/student_execution_os/web/static -name '*.js' -print0 | xargs -0 -n1 node --check

test:
	PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v

api:
	PYTHONPATH=src python -m unittest tests.web.web_api tests.web.test_auth_api -v

browser:
	PYTHONPATH=src python -m unittest tests.browser.browser_ui tests.browser.offline_e2e tests.browser.groups_e2e -v

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
	PYTHONPATH=src python -m student_execution_os notification-delivery-smoke
	PYTHONPATH=src python -m student_execution_os reliability-smoke

web:
	PYTHONPATH=src python -m student_execution_os.web.server --help >/dev/null

verify: restore static test api browser smoke web

mobile-sync:
	cd mobile && npm ci && npm run sync

apk: mobile-sync
	cd mobile/android && ./gradlew assembleDebug
	@echo "APK: mobile/android/app/build/outputs/apk/debug/app-debug.apk"
