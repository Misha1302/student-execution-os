.PHONY: restore static worker test api browser smoke verify web mobile-sync apk apk-release

restore:
	python -c "import fastapi, uvicorn, httpx, playwright"

static:
	PYTHONPATH=src python -m compileall -q src tests
	find src/student_execution_os/web/static -name '*.js' -print0 | xargs -0 -n1 node --check
	node tests/js/execution_overlay_cases.mjs
	node tests/js/plan_control_overlay_cases.mjs
	node tests/js/projects_overlay_cases.mjs
	node tests/js/work_routines_overlay_cases.mjs
	node tests/js/reflection_overlay_cases.mjs
	node tests/js/capture_kind_cases.mjs
	TZ=Europe/Moscow node tests/js/capture_semantic_cases.mjs
	node tests/js/capture_session_cases.mjs
	node tests/js/upcoming_cases.mjs
	TZ=UTC node tests/js/series_overlay_cases.mjs
	node --check deploy/cloudflare-groq-relay/src/index.js

worker:
	node --test deploy/cloudflare-groq-relay/test/*.test.js

test:
	PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py' -v

api:
	PYTHONPATH=src python -m unittest tests.web.web_api tests.web.test_auth_api -v

browser:
	PYTHONPATH=src python -m unittest tests.browser.browser_ui tests.browser.offline_e2e tests.browser.responsive_e2e tests.browser.today_e2e tests.browser.series_e2e tests.browser.academic_schedule_e2e tests.browser.connect_e2e tests.browser.groups_e2e tests.browser.final_student_e2e tests.browser.product_hardening_e2e -v

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

verify: restore static worker test api browser smoke web

mobile-sync:
	cd mobile && npm ci && npm run sync

apk: mobile-sync
	cd mobile/android && ./gradlew assembleDebug
	@echo "APK: mobile/android/app/build/outputs/apk/debug/app-debug.apk"

apk-release:
	@command -v gh >/dev/null 2>&1 || { \
		echo "ERROR: GitHub CLI (gh) is required"; \
		exit 1; \
	}
	@gh auth status >/dev/null 2>&1 || { \
		echo "ERROR: GitHub CLI is not authenticated; run: gh auth login"; \
		exit 1; \
	}
	@test "$$(git branch --show-current)" = "main" || { \
		echo "ERROR: production release must be dispatched from main"; \
		exit 1; \
	}
	@test -z "$$(git status --porcelain)" || { \
		echo "ERROR: working tree is not clean:"; \
		git status --short; \
		exit 1; \
	}
	@git fetch --quiet origin main
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse origin/main)" || { \
		echo "ERROR: local HEAD is not origin/main"; \
		echo "local:  $$(git rev-parse HEAD)"; \
		echo "remote: $$(git rev-parse origin/main)"; \
		echo "Commit/push/pull before releasing."; \
		exit 1; \
	}
	@echo "Dispatching production Android release for $$(git rev-parse --short HEAD)"
	@gh workflow run android-release.yml --ref main
