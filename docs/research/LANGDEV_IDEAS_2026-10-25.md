# LangDev → Student Execution OS: экономия без потери корректности действий

Дата: 2026-10-25. Baseline: `648c18d7dc679fe16c06a50b1e8477c814adf9ec`.
Статус: идеи и воспроизводимый offline probe, не изменение production-провайдера или тарификации.

## Проверенные факты и отвергнутая исходная гипотеза

Изучены ADR 0007, `agent/extraction.py`, `agent/providers.py`, `_context`/`_previous_session` в `agent/assistant.py`. LLM уже отделён от mutation authority: tool-less extraction, typed proposals, server-owned actor, expected version, idempotency и confirmation. Рекомендация «впервые отделить AI от действий» не соответствует коду.

Контекст тоже НЕ бесконечен: запрос obligations ограничен 80 строками, reminders — 40; предыдущая session ограничена 10 actions и привязана к account/principal/expiry. При этом SYSTEM_PROMPT фактически содержит 12 724 символа, 13 690 UTF-8 байт, 147 строк. Это НЕ число токенов. Prompt объединяет много семейств команд и правил корректировок.

В `providers.py` `_openai_usage` сохраняет общие prompt/completion/total, но не cache breakdown; `_anthropic_usage` учитывает только input/output, не отдельные cache read/write. Это статическое наблюдение дополнено исполняемым опытом ниже.

## SEOS-01. Сначала достоверный учёт, потом оптимизация cache

**Гипотеза:** текущая нормализация usage недостаточна для сравнения cold/warm requests; изменение формата/провайдера может выглядеть экономией, хотя часть расхода просто потеряна.

**Проверено:** `python docs/research/probe_usage_accounting.py` исполняет только три существующие pure adapter functions, выделенные через Python AST, на синтетических response objects; приложение и сетевые transports не импортируются. Результат в `usage-probe.json` с SHA-256 исходника.

Anthropic fixture: 50 обычных input + 1000 cache-read + 200 cache-write + 20 output. Текущий adapter вернул prompt=50, total=70, тогда как полный input по документированному формату — 1250. OpenAI-compatible fixture сохранил общий total=1270, но потерял cached=1000. Missing usage корректно вернулся None. Это подтверждает ограничение адаптеров, НЕ факт ошибочного реального списания денег: live caching/платёжные данные не проверялись.

**Предложение:** отдельный typed UsageRecord с provider/model/API shape, обычным input, cache read/write, output, total, known/unknown и источником измерения. Не складывать поля вслепую: у провайдеров разные определения общего input. Отдельно attempt id, repair/fallback route и финальный task id; aggregate должен сохранять расход неуспешных оплаченных попыток, если провайдер его сообщил. Отсутствующие данные не подменять нулём.

**Проверка следующего среза:** fixtures cold/read/write/mixed/missing/malformed/negative; совместимость существующих consumers; проверить quotas и отображение до изменения семантики total. Сначала additive telemetry, затем reviewed migration. Не исправлено автоматически именно потому, что это потенциально меняет смысл квот/учёта, а не только красивую статистику.

**Критерий пользы:** повторный запрос даёт объяснимый breakdown; сумма attempts не теряется; cost/success считается по применённым корректным действиям. Если cache недоступен используемому API, сохраняем корректную telemetry без обещания ускорения. P1, размер средний.

## SEOS-02. Контекст по семейству команды, с безопасным полным fallback

**Гипотеза:** для простого CREATE_NOTE или known UI operation полный каталог всех команд избыточен; можно уменьшить стабильный prompt и динамические данные без ухудшения корректности.

**Предложение:** общий неизменяемый safety/action contract + специализированные packets. Выбор из trusted UI mode либо отдельного проверяемого routing result; не считать совпадение русского keyword достоверным intent. Compound request, неизвестная команда, неоднозначная коррекция — полный packet. Всегда сохранять правила user edits, expected_version, account isolation, подтверждения, даты/timezone и зависимые actions. Кандидаты объектов должны быть server-authorized, модель не получает SQL или право менять scope.

**Особая проблема текущей реализации:** LIMIT 80 recent rows/40 reminders обеспечивает размер, но не гарантирует наличие нужного старого объекта. Поэтому оптимизация должна улучшать selection/target recall, а не просто уменьшать LIMIT. Нужен явный путь уточнения/authorized lookup без придумывания ID.

**Опыт:** замороженные 60 русскоязычных кейсов: простые, многокомандные, местоимения/исправления, одинаковые названия, даты/DST, просроченный version, запрос о старом объекте. Holdout отделён до настройки. Сравнить full, stable-prefix caching и routed packets при одной модели/version. Проверять semantic action set + сохранение user-edited fields + security negatives; не только совпадение текста. Считать billed tokens, round trips, clarification rate, accepted correct actions и latency.

**Опровержение:** missing target, неверная дата, дополнительный router-call съедает выигрыш, опасные действия менее надёжны. В таком случае стабильный prefix без routing лучше. P1, размер средний/большой, не реализовано.

## SEOS-03. Разные cache contracts для разных видов данных

**Гипотеза:** перенос exact-text result cache из простого классификатора в assistant даст неверные действия, даже если текст полностью совпал.

**Контрпример по текущему коду:** «перенеси на завтра» зависит от context.now/timezone, текущего объекта/version и предыдущих пользовательских правок. Две одинаковые строки не эквивалентны. `_previous_session` также проверяет scope и expiry. Кэш готового action по одному text нарушил бы эти предпосылки.

**Предложение:** кэшировать только стабильные инструкции/схемы через provider-supported prefix cache; canonical reads и action proposals не выдавать из такого кэша как исполненные действия. Для будущего immutable extraction cache ключ минимум content hash + extractor/prompt/schema/model version + tenant/privacy scope, а server-side ingestion всё равно повторно проверяет полномочия. Но если вход зависит от времени — добавить явный temporal contract либо запретить reuse.

**Опыт:** пары одинаковых текстов при разных now/version/tenant/session edits; cached instructions не должны менять результат авторизации и потребность в fresh interpretation. Для статичного extraction отдельно проверить cold/warm equivalence и отсутствие PII в логах. Победитель определяется cost per accepted correct action, а не token count одного успешного ответа.

**Опровержение:** cache даёт устаревший target или скрывает корректировку пользователя. Не использовать result memoization для mutation-bearing proposals. P1 как ограничение дизайна; runtime-код не изменён.

## Первичные источники

- Context Over Corpus — `xQd8bCu1bj8`, 06:15–11:40: structured routing и стабильный primer; перенос в SEOS является нашей гипотезой, а не результатом доклада.
- Teaching AI Agents to Work with MPS — `rOB4V547hU8`, 12:30–18:45: benchmark quality/round trips, не безусловное добавление tools.
- Domain Knowledge Debt — `05i4vScgH4M`, 14:35–15:00: модель не должна заменять owner решения; 22:30 — сами авторы не заявляют эмпирическую валидацию гипотезы.
- OpenAI Prompt caching, проверено 2026-10-25: https://developers.openai.com/api/docs/guides/prompt-caching . Формат usage и cache options проверять для конкретного API/модели, не переносить из Responses в compatibility chat автоматически.
- Anthropic Prompt caching / Tracking cache performance, проверено 2026-10-25: https://platform.claude.com/docs/en/build-with-claude/prompt-caching . Общий input включает ordinary input + cache read + cache creation; write и read различаются.

Слайды не проверялись; ASR имена/цифры не считаются точными benchmark. Probe — bounded executable evidence, НЕ полный test suite Student Execution OS. Коммерческие тарифы и реальные счета не измерены.
