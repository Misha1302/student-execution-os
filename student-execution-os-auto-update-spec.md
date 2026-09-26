# Student Execution OS — Техническое задание на систему автоматических обновлений

**Версия ТЗ:** 1.0.0  
**Статус:** implementation-ready draft  
**Приоритет:** отдельная инфраструктурная фича, не часть collaborative groups  

---

## 1. Цель

Добавить в Student Execution OS безопасный, управляемый и удобный механизм доставки новых версий приложения без обязательной публикации в Google Play, Microsoft Store, Mac App Store или другом магазине приложений.

Пользователь должен получать новую стабильную версию с минимальным количеством ручных действий, а команда проекта должна иметь возможность:

- выпускать новую версию;
- постепенно раскатывать её пользователям;
- останавливать проблемный rollout;
- различать `stable` и `beta`;
- принудительно требовать обновление только когда это действительно необходимо;
- видеть технический результат проверки/скачивания/установки;
- не терять пользовательские данные при обновлении;
- не зависеть от конкретного release-hosting provider.

На первом этапе приложение **не предполагается публиковать в Google Play**.

---

## 2. Главный архитектурный принцип

Система обновлений состоит из двух независимых уровней:

```text
Update Policy / Release Metadata
             │
             ▼
   Platform Update Adapter
             │
      ┌──────┼────────┐
      ▼      ▼        ▼
  Desktop  Android   Web/PWA
```

### Update Policy отвечает за

- какая версия доступна;
- для какого канала;
- для какой платформы/архитектуры;
- является ли обновление обязательным;
- какой минимальный поддерживаемый клиент;
- rollout percentage;
- release notes;
- integrity/trust metadata.

### Platform Update Adapter отвечает за

- скачать подходящий артефакт;
- проверить его;
- безопасно применить обновление;
- перезапустить приложение, если требуется;
- сообщить результат canonical update service.

Нельзя смешивать release policy, download transport и platform-specific installation в одном giant service.

---

## 3. Рекомендуемая архитектура для текущего этапа

### 3.1. Desktop

Если desktop-клиент построен на .NET/C#/C++ и совместим с Velopack, использовать **Velopack** как основной installer/update adapter для Windows, macOS и Linux вместо написания собственного бинарного updater.

Причины:

- поддержка Windows/macOS/Linux;
- release channels;
- full и delta packages;
- checksum verification;
- update locking;
- apply/restart flow;
- возможность размещать feed на обычном HTTPS/static hosting;
- отсутствие зависимости от app store.

Application code должен зависеть не напрямую от Velopack во всех слоях, а от project-owned abstraction, например:

```text
IAppUpdateService
    │
    ├── DesktopUpdateAdapter
    │      └── Velopack
    ├── AndroidUpdateAdapter
    └── WebUpdateAdapter
```

Это позволяет позднее заменить конкретный updater без переписывания UI и бизнес-логики update policy.

### 3.2. Initial hosting

Для первой версии разрешается использовать:

```text
GitHub Releases / public static release hosting
```

как хранилище build artifacts.

GitHub не является частью domain architecture и не должен быть hardcoded во всех слоях.

Update source должен быть заменяемым:

```text
IUpdateSource
- StaticHttp
- GitHubReleaseHosting
- S3CompatibleStorage (future)
- OwnCDN (future)
```

Если основной repository приватный, **не встраивать GitHub PAT/token в приложение**.

В таком случае использовать отдельный публично читаемый release endpoint/bucket либо backend-proxy, не раскрывающий privileged credentials.

---

## 4. Scope первой версии

Первая production-capable версия update system должна поддерживать:

- проверку наличия обновления;
- `stable` и `beta` channels;
- manual `Check for updates`;
- автоматическую периодическую проверку;
- background download там, где это безопасно и поддерживается;
- verified artifact integrity;
- release notes;
- установка при перезапуске или через явное действие;
- mandatory/minimum-supported-version policy;
- staged rollout;
- pause/withdraw release;
- telemetry update state без пользовательского содержимого;
- Windows;
- Linux, если desktop Linux уже поддерживается;
- macOS, если desktop macOS уже поддерживается;
- Android sideload update flow, если Android client существует;
- Web/PWA refresh/update flow, если Web/PWA client существует.

Отсутствующая в текущем проекте платформа не должна блокировать реализацию updater для реально существующих клиентов.

---

## 5. Что не входит в MVP

Не реализовывать без отдельного решения:

- собственный general-purpose package manager;
- peer-to-peer distribution;
- torrent distribution;
- background silent installation через обход ограничений ОС;
- device management / enterprise MDM;
- сложный multi-tenant release server;
- бинарные patch algorithms собственного производства;
- собственную криптографию;
- хранение update artifacts внутри основной БД;
- обязательную интеграцию с Google Play;
- App Store publishing automation;
- silent downgrade как обычную функцию;
- server-controlled arbitrary executable commands на клиенте.

---

## 6. Версионирование приложения

Использовать SemVer 2 для human-visible application version:

```text
MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]
```

Примеры:

```text
1.4.0
1.4.1
1.5.0-beta.3
1.5.0+build.184
```

Кроме SemVer должен существовать монотонный machine build identifier там, где платформа этого требует:

```text
build_number: 184
```

### Правила

- `stable` не должен автоматически получать prerelease build;
- `beta` может получать prerelease;
- один immutable release version нельзя заменять другими bytes;
- если артефакт изменился — выпускается новая версия/build;
- нельзя перезаливать другой binary под тем же version/hash identity;
- version сравнение выполняется typed parser, а не строковым сравнением.

---

## 7. Release channels

Минимум:

```text
STABLE
BETA
```

В будущем допустимы:

```text
NIGHTLY
INTERNAL
```

но они не нужны для MVP.

### STABLE

- default для обычного пользователя;
- получает только проверенные release builds;
- rollback/downgrade автоматически не включается;
- staged rollout используется по умолчанию для существенных версий.

### BETA

- opt-in пользователя;
- может получать prerelease;
- UI должен явно показывать, что канал тестовый;
- пользователь может вернуться на stable;
- если возврат требует downgrade, он выполняется только явным controlled flow.

Канал пользователя хранится как local application preference и не должен зависеть от аккаунта, если для этого нет отдельной продуктовой причины.

---

## 8. Update policy

Update policy должен быть отдельной typed моделью.

Пример логической структуры:

```text
UpdatePolicy
- schema_version
- sequence
- generated_at
- expires_at
- channel
- latest_version
- minimum_supported_version?
- rollout
- releases[]
- signature_metadata
```

Для каждой версии:

```text
UpdateRelease
- version
- build_number
- channel
- published_at
- status
- severity
- release_notes
- minimum_os_versions?
- minimum_api_version?
- artifacts[]
```

Для каждого artifact:

```text
UpdateArtifact
- platform
- architecture
- artifact_kind
- url
- size_bytes
- sha256
- updater_metadata?
```

`UpdatePolicy` является **единственным application-level владельцем решения о том, какую версию клиенту разрешено устанавливать**. Platform-specific feed (например `releases.{channel}.json` Velopack) является только transport/package index и не имеет права самостоятельно переопределять target, выбранный signed policy.

Нормативный flow:

```text
signed UpdatePolicy
       ↓
policy evaluator выбирает exact target version
       ↓
platform feed/adapter находит package именно этой версии
       ↓
download
       ↓
повторная проверка size/hash из signed policy
       ↓
apply
```

Если platform framework по умолчанию выбирает `latest`, adapter обязан ограничить его exact target, выбранным policy layer.

Не создавать две независимые сущности, обе претендующие быть владельцем `latest version` или update eligibility.

---

## 9. Release status

Release status минимум:

```text
DRAFT
AVAILABLE
PAUSED
WITHDRAWN
```

### DRAFT

Клиент не видит релиз.

### AVAILABLE

Релиз может быть выбран policy evaluator.

### PAUSED

Новые клиенты временно не должны начинать обновление до этой версии.

Уже скачанный update не должен автоматически применяться, если клиент успел получить актуальный `PAUSED` policy до установки.

### WITHDRAWN

Релиз больше не предлагается новым клиентам.

`WITHDRAWN` не означает автоматический downgrade уже обновившихся пользователей.

---

## 10. Severity обновления

Не смешивать severity и mandatory policy.

```text
NORMAL
IMPORTANT
SECURITY
CRITICAL
```

Severity отвечает за заметность и приоритет коммуникации.

Mandatory policy отдельно отвечает за то, можно ли продолжать работу на старой версии.

Пример:

```text
SECURITY + optional now
CRITICAL + mandatory_after=2026-10-15T00:00:00Z
```

---

## 11. Optional, required и minimum-supported update

Поддержать три режима.

### OPTIONAL

Обычное обновление.

Пользователь может:

- установить сейчас;
- установить при закрытии/перезапуске;
- напомнить позже.

### REQUIRED_AFTER

До указанного времени приложение продолжает работать, но показывает повышенно заметное предложение обновиться.

После deadline применяется minimum-version policy.

### UNSUPPORTED_CLIENT

Используется только если текущая версия действительно несовместима с сервером либо имеет критическую уязвимость/дефект.

При этом:

- не удалять локальные данные;
- offline data остаются доступны насколько это безопасно;
- блокировать только те online/sync операции, которые невозможно безопасно выполнить;
- пользователь видит понятную причину и путь обновления;
- updater остаётся доступным даже если основной API больше не поддерживает клиент.

Нельзя использовать forced update просто для увеличения adoption новой версии.

---

## 12. Отдельный update endpoint

Update check не должен зависеть от успешной работы основного application API.

Иначе старый клиент может оказаться в состоянии:

```text
API says "client too old"
        +
client cannot reach updater because updater uses same incompatible API
```

Update metadata и artifacts должны быть доступны через независимый стабильный HTTPS endpoint/feed.

Пример логически:

```text
https://updates.example.com/student-os/...
```

Фактически на первом этапе это может быть GitHub/static hosting.

---

## 13. Проверка обновлений

Минимальные triggers:

```text
- startup check
- periodic check while app is running
- manual check
- reconnect after sufficiently long offline interval
```

### Startup

Не блокировать startup UI сетевым запросом updater.

Flow:

```text
App starts
   ↓
local UI available
   ↓
background update check
```

### Periodic

Рекомендуемый default:

```text
примерно раз в 6 часов
```

с небольшим deterministic/randomized jitter, чтобы все клиенты не обращались к endpoint одновременно.

Не проверять обновления каждые несколько минут.

### Manual

В Settings/About:

```text
Текущая версия: 1.4.2
Канал: Stable
[Проверить обновления]
```

Manual check должен обходить обычный local freshness cache, но не security validation.

---

## 14. Update state machine

Canonical client state минимум:

```text
IDLE
CHECKING
UP_TO_DATE
AVAILABLE
DOWNLOADING
DOWNLOADED
READY_TO_INSTALL
APPLYING
RESTART_REQUIRED
FAILED
```

Дополнительные states допустимы platform adapter-у, но UI должен получать нормализованный canonical state.

Основные transitions:

```text
IDLE -> CHECKING
CHECKING -> UP_TO_DATE | AVAILABLE | FAILED
AVAILABLE -> DOWNLOADING
DOWNLOADING -> DOWNLOADED | FAILED
DOWNLOADED -> READY_TO_INSTALL
READY_TO_INSTALL -> APPLYING
APPLYING -> RESTART_REQUIRED | IDLE(new version) | FAILED
```

Cancellation download:

```text
DOWNLOADING -> AVAILABLE
```

если platform adapter поддерживает безопасную отмену.

---

## 15. Download policy

По умолчанию:

- update check происходит автоматически;
- stable update может автоматически скачиваться в фоне;
- установка не должна неожиданно закрывать приложение;
- apply выполняется при явном действии либо при безопасном controlled restart.

Если ОС предоставляет reliable metered-network information, auto-download должен уважать её.

При отсутствии такой возможности пользовательская настройка может быть:

```text
Automatically download updates: ON/OFF
```

Не вводить сложную сетевую policy систему в MVP.

---

## 16. Install policy

Нельзя перезапускать приложение без предупреждения, если пользователь активно работает.

Основной UX:

```text
Обновление 1.5.0 готово

[Перезапустить и обновить]
[При следующем запуске]
```

Для небольшой optional версии можно использовать ненавязчивый badge/toast.

Для critical/security версии — persistent update card/banner.

---

## 17. Защита пользовательских данных

Application install directory не является местом хранения пользовательских данных.

При обновлении не должны теряться:

- локальная БД;
- offline mutations;
- planner state;
- settings;
- tokens/credentials;
- cached user-owned documents;
- logs, если они должны переживать update;
- rollback-relevant migration metadata.

Разделить:

```text
APP_BINARY_DIR
APP_DATA_DIR
APP_CACHE_DIR
APP_LOG_DIR
```

Updater имеет право заменять только installation/bundle files и собственный update cache.

---

## 18. Database migrations

Auto-update и schema migrations должны проектироваться совместно.

Основной инвариант:

```text
Application binary update must not make user data unrecoverable.
```

### Требования

- migration выполняется после подтверждённого перехода на новый binary;
- migration должна быть transactional где возможно;
- destructive migration требует отдельного обоснования;
- перед необратимой migration нужна strategy recovery/backup;
- migration version хранится независимо от app version;
- повторный запуск migration idempotent либо корректно определяется как already applied;
- crash посередине migration не должен оставлять БД в неопределённом состоянии.

### Rollback compatibility

Не обещать binary rollback через schema boundary, если старая версия уже не умеет читать новую schema.

Для каждой release migration определить:

```text
rollback_compatibility:
- FULL
- BINARY_ONLY
- NOT_SUPPORTED
```

Если rollback несовместим со schema, release pipeline не должен рекламировать автоматический rollback как безопасный.

---

## 19. Security model

Update system является security-sensitive infrastructure.

Минимально обязательно:

- HTTPS;
- artifact checksum verification;
- platform code signing там, где доступно;
- immutable version artifacts;
- no embedded privileged hosting credentials;
- anti-rollback policy;
- metadata freshness/expiry;
- bounded artifact size before download;
- canonical platform/architecture matching;
- explicit release identity;
- безопасная обработка redirect;
- защита от path traversal при unpacking;
- atomic replacement где возможно;
- update operation lock;
- проверка installer/updater errors.

Нельзя считать HTTPS единственным механизмом доверия.

---

## 20. Signed metadata

Для production distribution update policy должна иметь cryptographic authenticity независимо от hosting provider.

### Практический MVP

Использовать стандартную криптографическую библиотеку и detached/embedded signature над canonical serialized metadata.

Предпочтительный algorithm:

```text
Ed25519
```

Клиент содержит public verification key.

Private release signing key:

- не хранится в source repository;
- не встраивается в client;
- хранится в защищённом CI secret/HSM/offline signing environment;
- доступен только release pipeline.

### Подписываемые поля

Подпись должна покрывать как минимум:

- schema version;
- metadata sequence;
- generated/expires timestamps;
- channel;
- release version/build;
- platform/arch;
- artifact URL или immutable artifact identity;
- artifact SHA-256;
- artifact size;
- rollout policy;
- minimum supported version;
- mandatory policy.

Нельзя подписывать только hash файла отдельно от policy, иначе attacker может менять routing/policy metadata.

### Важное ограничение

Не придумывать собственный crypto algorithm или custom signature primitive.

Если проект переходит к более высокому security requirement, заменить/расширить этот слой полноценным TUF-compatible metadata workflow, а не бесконечно усложнять самописный формат.

---

## 21. Metadata freshness и anti-rollback

Metadata содержит:

```text
sequence
expires_at
```

Клиент хранит maximum successfully accepted sequence для данного channel/source.

По умолчанию отклонять metadata, если:

```text
sequence < last_accepted_sequence
```

или metadata expired.

Это не относится к явно подтверждённому controlled rollback flow.

Нельзя позволять remote endpoint молча заставить клиента перейти на более старый release.

---

## 22. Key rotation

Public update key нельзя считать вечным.

Для MVP реализовать минимум подготовку к rotation:

```text
trusted_key_ids[]
```

и поддержку нескольких встроенных verification keys.

Rotation выполняется в две фазы:

1. выпустить клиент, доверяющий old + new key;
2. дождаться достаточного adoption;
3. начать подписывать new key;
4. позднее удалить old key новой версией.

Emergency key compromise должен иметь документированный manual incident procedure.

Не пытаться удалённо принять совершенно новый trust root, подписанный только самим скомпрометированным online key.

---

## 23. Platform code signing

### Windows

Production installer/application рекомендуется подписывать code-signing certificate.

Unsigned build допустим только для development/internal testing.

### macOS

Production distribution вне Mac App Store должна учитывать code signing и notarization requirements платформы.

### Android

Каждый APK update должен быть подписан тем же application signing identity, который позволяет ОС принять его как обновление установленного приложения.

Signing key нельзя менять без заранее спроектированного migration path.

### Linux

Artifact integrity должна проверяться update metadata/hash/signature даже если платформа не предоставляет аналог Windows/macOS code signing UX.

---

## 24. Desktop implementation adapter

Если текущий desktop stack совместим — рекомендуемый adapter:

```text
VelopackDesktopUpdateAdapter
```

Он должен использовать framework для:

- package/release feed как transport index;
- поиска **exact target version**, уже выбранной `UpdatePolicy`;
- download;
- delta/full selection;
- framework checksum validation;
- apply;
- restart;
- update locking.

После framework download и **до apply** project adapter дополнительно сверяет downloaded full target artifact с `size/hash`, подписанными canonical `UpdatePolicy`. Package feed сам по себе не является источником доверия и не выбирает product target.

Project code отвечает за:

- policy evaluation;
- UX;
- channel preference;
- staged rollout eligibility;
- mandatory policy;
- telemetry;
- integration with application lifecycle.

Не копировать внутренний Velopack update engine в собственный код.

---

## 25. Windows flow

Рекомендуемый пользовательский flow:

```text
Install Student Execution OS Setup.exe
        ↓
application installed per-user where possible
        ↓
signed policy check выбирает exact stable target
        ↓
platform updater получает package именно target version
        ↓
new package downloaded + verified
        ↓
user chooses restart/update
        ↓
updater replaces application version
        ↓
new version starts
```

Предпочитать per-user installation, если приложение не требует machine-wide privileges.

Это уменьшает необходимость UAC/elevation при обычных обновлениях.

---

## 26. Linux flow

Если используется Velopack/AppImage:

```text
StudentExecutionOS.AppImage
        ↓
check update
        ↓
download package
        ↓
replace AppImage atomically where possible
        ↓
restart
```

Если AppImage расположен в privileged directory, platform adapter может потребовать elevation.

App data не хранить внутри AppImage/AppDir.

---

## 27. macOS flow

Если macOS поддерживается:

- `.app` bundle;
- code signing;
- notarization;
- updater replaces bundle через framework-supported flow;
- application data лежат вне bundle;
- пользователь заранее информируется о restart.

Не пытаться обходить системную security model macOS.

---

## 28. Android без Google Play

Android distribution в MVP может выполняться через APK с официальной страницы проекта/static release hosting.

Flow:

```text
App checks signed update metadata
        ↓
new compatible APK available
        ↓
user sees release/update UI
        ↓
APK downloaded and verified
        ↓
Android PackageInstaller flow
        ↓
OS may request user confirmation
        ↓
updated app starts
```

### Критический UX-инвариант

Не обещать fully silent Android update.

Приложение должно быть готово к системному `user action required` flow.

Если ОС/installer policy позволяет update без дополнительного confirmation, это оптимизация platform adapter, а не обязательное предположение domain layer.

### Unknown sources

Если устройство не разрешает установку обновлений от текущего source, показать короткую системно-корректную инструкцию и открыть соответствующий OS flow.

Не просить пользователя глобально отключать Android security mechanisms.

---

## 29. Web / PWA

Web-клиент не использует binary updater desktop/mobile.

Deployment должен быть atomic на сервере/CDN.

Для PWA/service worker:

- обнаружить новую application shell version;
- не перезагружать страницу посреди пользовательской работы;
- показать `Новая версия готова`;
- применить после user action либо безопасного reload;
- не потерять unsaved/offline mutations.

Пример:

```text
Новая версия Student Execution OS готова.
[Обновить сейчас]
```

При несовместимости API использовать тот же minimum-supported-version semantics.

---

## 30. Staged rollout

Каждый stable release должен поддерживать rollout percentage:

```text
0%
5%
25%
50%
100%
```

Конкретные числа могут меняться.

### Eligibility

Нельзя выбирать rollout случайно заново при каждом check.

Использовать стабильный anonymous installation identifier:

```text
bucket = H(installation_id, release_id) % 10000
```

Release eligible, если bucket попадает в rollout range.

Это обеспечивает deterministic cohort assignment.

Installation ID:

- генерируется локально;
- не должен быть hardware fingerprint;
- не должен использовать IMEI/MAC/serial number;
- может быть сброшен при полной переустановке;
- используется только для product infrastructure/rollout, если privacy policy это допускает.

---

## 31. Pause rollout

Release operator должен иметь возможность изменить:

```text
rollout = 25% -> 0%
status = PAUSED
```

без выпуска нового client binary.

Новые клиенты перестают начинать update.

Уже установленная версия не downgrade-ится автоматически.

Уже скачанный, но ещё не применённый release должен повторно проверить актуальную policy перед apply, если metadata достаточно устарела или release имеет повышенный риск.

---

## 32. Rollback strategy

Различать два разных понятия.

### 32.1. Rollout rollback

Плохой release перестают раздавать:

```text
AVAILABLE -> PAUSED/WITHDRAWN
```

и публикуют fixed forward release:

```text
1.5.0 bad
1.5.1 fixed
```

Это основной production rollback strategy.

### 32.2. Client binary downgrade

Переход уже обновившегося клиента на более старый binary.

Это исключительная recovery operation.

Разрешать только если:

- updater framework поддерживает это безопасно;
- database schema совместима;
- update policy явно разрешает target version;
- artifact всё ещё trusted;
- операция не приводит к потере данных.

Не включать unconditional automatic downgrade.

---

## 33. Crash-after-update detection

Для desktop клиента добавить lightweight post-update health marker.

Пример:

```text
pending_update = 1.5.0
launch_attempt = 1
startup_health = pending
```

После успешного прохождения критической startup phase:

```text
startup_health = healthy
```

Если новая версия несколько раз подряд падает до healthy marker, приложение/updater должен:

- сохранить diagnostic state;
- не создавать бесконечный restart loop;
- предложить recovery;
- при наличии доказанно безопасного rollback path разрешить rollback;
- иначе дать ссылку/путь на reinstall known-good version без удаления user data.

Не считать любой поздний runtime crash признаком неудачного обновления.

---

## 34. Update transaction

Логически apply должен вести себя как transaction:

```text
1. acquire update lock
2. validate target metadata
3. validate package hash/signature
4. verify sufficient disk space
5. prepare new version
6. close application gracefully
7. replace/switch application atomically where platform permits
8. start new version
9. mark launch health
10. cleanup stale package cache later
```

Если ошибка произошла до шага переключения версии, текущая версия должна оставаться запускаемой.

---

## 35. Concurrency

Не разрешать одновременно:

- два update downloads для одного target;
- два apply operations;
- installer + application update apply;
- несколько processes, одновременно меняющих installation directory.

Использовать updater/framework lock.

UI второго процесса показывает существующее update state, а не начинает независимую установку.

---

## 36. Resume и interrupted downloads

Если выбранный framework поддерживает resume — использовать его.

При interrupted download:

- partial artifact не считается valid;
- перед apply всегда повторная integrity verification;
- corrupted cache удаляется либо скачивается заново;
- не запускать installer из `.partial`/temporary file.

---

## 37. Delta updates

Delta updates являются optimization, а не canonical requirement.

Если framework поддерживает их надёжно — включить.

При любой ошибке delta reconstruction должен использоваться fallback на full package.

Correctness не должна зависеть от availability delta package.

---

## 38. Release notes

Каждый user-facing release содержит:

```text
version
short summary
important changes
migration/compatibility notice, если нужен
security note, если раскрытие безопасно
```

Не показывать raw Git commit list как основной UX.

Пример:

```text
Версия 1.5.0

• Добавлены учебные группы
• Улучшена синхронизация Agenda
• Исправлена ошибка напоминаний после переноса события
```

---

## 39. Settings UI

Минимально:

```text
Обновления

Версия: 1.4.2
Канал: Stable

[✓] Проверять обновления автоматически
[✓] Скачивать обновления автоматически

[Проверить обновления]
```

Beta channel:

```text
Канал обновлений
(o) Stable
( ) Beta
```

Переключение на Beta требует short confirmation.

Возврат Beta -> Stable с downgrade требует отдельного confirmation и compatibility check.

---

## 40. Update notification UX

### Normal

Небольшой toast/card:

```text
Доступна версия 1.5.0
[Что нового] [Обновить]
```

### Downloaded

```text
Обновление готово к установке
[Перезапустить]
[При следующем запуске]
```

### Critical

Persistent banner/card, но без агрессивного modal loop до mandatory boundary.

### Mandatory

Если версия более не поддерживается:

```text
Нужно обновить Student Execution OS

Эта версия больше не совместима с сервером.
Ваши локальные данные сохранены.

[Обновить]
```

Не использовать пугающие формулировки без необходимости.

---

## 41. Offline behavior

Отсутствие сети не должно мешать открыть приложение только потому, что update check невозможен.

Если metadata не удалось получить:

```text
current app continues normally
```

кроме случая, когда уже локально имеется ранее проверенная policy, однозначно запрещающая небезопасную online operation.

Не делать remote update endpoint single point of failure для offline planner.

---

## 42. Server compatibility contract

Backend должен уметь сообщать минимум:

```text
minimum_client_version
recommended_client_version?
```

но canonical install target определяется update policy, а не произвольным API error.

Если API получает unsupported client:

- вернуть machine-readable error;
- приложение направляет пользователя в updater;
- update check использует независимый endpoint;
- local/offline data не удаляются.

---

## 43. API compatibility window

Backend releases должны по возможности поддерживать разумное окно старых клиентов, чтобы staged rollout был возможен.

Нельзя выпускать backend и одновременно мгновенно ломать все предыдущие clients без emergency reason.

Deployment ordering для breaking compatibility:

```text
1. backend становится совместимым со старым + новым client
2. публикуется новый client
3. проходит rollout
4. adoption достигает достаточного уровня
5. только затем повышается minimum_client_version
```

---

## 44. Telemetry

Собирать только operational update events, если telemetry разрешена продуктовой privacy policy:

```text
UPDATE_CHECK_STARTED
UPDATE_CHECK_SUCCEEDED
UPDATE_AVAILABLE
UPDATE_DOWNLOAD_STARTED
UPDATE_DOWNLOAD_COMPLETED
UPDATE_DOWNLOAD_FAILED
UPDATE_APPLY_STARTED
UPDATE_APPLY_SUCCEEDED
UPDATE_APPLY_FAILED
UPDATE_HEALTHY_START
```

Допустимые dimensions:

- app version;
- target version;
- platform;
- architecture;
- channel;
- updater error code;
- coarse rollout cohort;
- duration;
- artifact type.

Не отправлять:

- задачи пользователя;
- расписание;
- названия предметов;
- содержимое Agenda;
- документы;
- hardware identifiers;
- access tokens.

---

## 45. Logging

Local update log должен позволять диагностировать:

- current version;
- target version;
- channel;
- metadata fetch result;
- signature validation result;
- artifact hash result;
- download/apply state;
- platform updater error;
- timestamps.

Нельзя логировать credentials или signed download secret query parameters, если такие появятся в будущем.

---

## 46. Release pipeline

Release pipeline должен быть deterministic и fail-closed.

Рекомендуемый порядок:

```text
1. checkout exact release commit
2. restore dependencies
3. build
4. run tests
5. produce platform artifacts
6. package updater artifacts
7. code-sign/notarize where required
8. calculate hashes/sizes
9. run installation/update smoke tests
10. upload immutable artifacts
11. verify uploaded artifact hashes
12. generate release metadata/feed
13. sign metadata
14. publish metadata LAST
15. begin rollout
```

Критический принцип:

```text
Artifacts first, discovery metadata last.
```

Клиент никогда не должен увидеть AVAILABLE release, файлы которого ещё не опубликованы полностью.

---

## 47. Release authority

Обычный CI build не равен production release.

Отдельно различать:

```text
BUILD
PACKAGE
PUBLISH_ARTIFACTS
PROMOTE_RELEASE
```

Production signing/promotion требует явно защищённого CI environment/approval согласно возможностям repository platform.

Pull request из недоверенного fork не должен иметь доступ к signing secrets.

---

## 48. Artifact naming

Имена должны быть deterministic и однозначными.

Примеры:

```text
student-execution-os-1.5.0-win-x64-setup.exe
student-execution-os-1.5.0-win-arm64-setup.exe
student-execution-os-1.5.0-linux-x64.AppImage
student-execution-os-1.5.0-macos-arm64.pkg
student-execution-os-1.5.0-android.apk
```

Не использовать `latest.exe` как единственную immutable identity.

Alias `latest` допустим только как discovery convenience.

---

## 49. Architecture selection

Update client должен выбирать artifact только по canonical runtime platform/architecture mapping.

Минимальные target identities, если поддерживаются проектом:

```text
win-x64
win-arm64
linux-x64
linux-arm64
osx-x64
osx-arm64
android-arm64
```

Не угадывать architecture по filename substring в UI layer.

---

## 50. Insufficient disk space

До apply/download, когда размер известен, проверить достаточность диска настолько, насколько это позволяет платформа.

При недостатке:

```text
Недостаточно места для обновления.
Нужно освободить примерно N МБ.
```

Не удалять user data ради освобождения места.

Можно очистить только безопасный stale update cache.

---

## 51. Failure handling

Update failure не должен повреждать текущую working installation.

Типовые classes:

```text
NETWORK_ERROR
METADATA_INVALID
METADATA_EXPIRED
SIGNATURE_INVALID
HASH_MISMATCH
UNSUPPORTED_PLATFORM
INSUFFICIENT_DISK
UPDATE_LOCKED
INSTALLER_FAILED
PERMISSION_REQUIRED
MIGRATION_FAILED
UNKNOWN
```

UI показывает human-readable message.

Logs сохраняют typed error.

Не показывать пользователю raw stack trace как основной error text.

---

## 52. Retry policy

Network checks/downloads могут retry с exponential backoff.

Не retry бесконечно installer/apply errors без изменения состояния.

После двух-трёх одинаковых apply failures:

- прекратить автоматические attempts;
- показать recovery action;
- сохранить diagnostics.

---

## 53. Manual recovery

Всегда должен существовать manual recovery path:

```text
Скачать последнюю стабильную версию
```

с официального project release endpoint/site.

Reinstall поверх текущей версии не должен удалять user data, если platform packaging позволяет это обеспечить.

Recovery download должен использовать те же trusted release identities.

---

## 54. Uninstall

Updater не должен менять существующую uninstall semantics неожиданным образом.

Удаление приложения и удаление пользовательских данных должны быть разными explicit choices, если это соответствует текущему продукту.

Update никогда не должен запускать uninstall-user-data flow.

---

## 55. Observability для rollout

Перед повышением rollout желательно видеть минимум:

```text
check success rate
artifact download success rate
apply success rate
healthy-start rate
crash/startup failure delta
```

Если telemetry infrastructure пока отсутствует, staged rollout всё равно реализовать, а автоматическое promotion не включать.

Promotion выполняется вручную после проверки доступной диагностики.

---

## 56. Automatic rollout promotion

Не входит в первую версию.

MVP:

```text
5% -> manual review -> 25% -> manual review -> 100%
```

Позже можно добавить automatic promotion при соблюдении metrics thresholds.

Не создавать auto-promotion до появления достоверной telemetry.

---

## 57. Security incident flow

Должен существовать documented operator flow:

### Bad application release

```text
pause release
publish fixed forward release
increase visibility/severity if needed
```

### Hosting compromise

```text
clients reject invalid metadata/signatures
rotate hosting credentials
republish trusted artifacts
```

### Signing key suspected compromised

```text
stop publishing
invoke key-rotation incident procedure
publish client/recovery using retained trusted root strategy
```

Не импровизировать key recovery после инцидента без заранее определённого trust path.

---

## 58. Privacy

Update check должен передавать только минимальные технические данные.

Предпочтительно update selection делать client-side по опубликованному feed.

Если backend selection endpoint всё же используется, минимум:

```text
platform
architecture
current_version
channel
anonymous rollout id/bucket
```

Не связывать update check с академическими данными пользователя.

---

## 59. Recommended domain/API abstractions

Имена могут быть адаптированы к существующей architecture.

Пример:

```text
IAppUpdateService
- GetCurrentVersion()
- CheckForUpdates()
- Download(update)
- Apply(update)
- ApplyAndRestart(update)
- GetState()

IUpdatePolicyProvider
- GetPolicy(channel)

IPlatformUpdateAdapter
- CheckCompatibility(release)
- Download(artifact)
- Verify(artifact)
- Apply(artifact)

IUpdateStateStore
- current_channel
- last_check_at
- last_accepted_metadata_sequence
- pending_release
- update_health_marker
```

Platform framework classes не должны проникать в unrelated domain/application code.

---

## 60. UI integration

Update UI должен использовать существующие:

- settings screens;
- notifications/toasts;
- dialogs/action sheets;
- localization;
- app lifecycle/restart infrastructure.

Не создавать отдельное визуально чуждое updater-приложение, кроме platform-required installer UI.

---

## 61. i18n

Все user-facing update strings:

- RU;
- EN.

Не хранить локализованный текст release policy как единственный semantic source.

Machine fields остаются typed; release notes могут иметь locale variants.

---

## 62. Feature flags

Минимум operational flags:

```text
updates.enabled
updates.auto_check
updates.auto_download
updates.beta_channel_available
```

Critical security verification нельзя отключать обычным remote feature flag.

Нельзя иметь flag вида:

```text
skip_signature_validation=true
```

в production configuration.

---

## 63. Test mode

Нужен отдельный local/test update source.

Developer должен иметь возможность проверить:

```text
1.0.0 -> 1.0.1
```

без публикации production release.

Test mode не должен использовать production signing key.

Debug build по умолчанию не должен случайно обновляться из production stable feed.

---

## 64. Unit tests

Обязательны минимум:

### Version policy

- newer stable выбирается;
- older release не выбирается автоматически;
- beta не попадает stable client;
- semver comparison корректен;
- incompatible platform ignored;
- incompatible OS ignored;
- minimum supported version определяется корректно.

### Rollout

- один installation id стабильно попадает в тот же bucket;
- изменение rollout 5 -> 25 расширяет cohort;
- rollout 0 не предлагает release;
- rollout 100 предлагает всем eligible clients.

### Metadata

- valid signature accepted;
- invalid signature rejected;
- modified artifact hash rejected;
- expired metadata rejected;
- lower metadata sequence rejected;
- wrong channel rejected;
- wrong platform/arch rejected.

### State machine

- valid transitions;
- invalid transitions;
- failed download не становится READY_TO_INSTALL;
- failed verification не вызывает apply.

---

## 65. Integration tests

Минимум:

- update check против local HTTP fixture;
- successful full download;
- corrupted download;
- interrupted/retry download;
- missing artifact;
- metadata points to unavailable artifact;
- concurrent update lock;
- apply lifecycle через platform test harness;
- application data preserved;
- migration preserved;
- restart into new version;
- paused release not applied after policy refresh;
- manual check ignores freshness cache.

---

## 66. End-to-end desktop tests

На поддерживаемых CI/VM platforms проверить реальный packaged application.

### Scenario A — normal update

```text
install 1.0.0
publish 1.0.1
check
 download
apply
restart
assert running 1.0.1
assert user data preserved
```

### Scenario B — corrupt package

```text
publish metadata
corrupt artifact
client downloads
verification fails
1.0.0 remains runnable
```

### Scenario C — interrupted update

```text
start download
interrupt process/network
restart client
recover/redownload
install once
```

### Scenario D — staged rollout

```text
release 5%
non-eligible installation sees no update
eligible installation sees update
change to 100%
all eligible platform clients see update
```

### Scenario E — paused release

```text
release available
pause before apply
fresh policy check
client does not apply automatically
```

---

## 67. Android tests

Если Android client существует:

- APK signed with expected identity;
- update metadata signature valid;
- wrong certificate APK rejected by OS/update flow;
- PackageInstaller user-action-required handled;
- user cancellation handled;
- downloaded APK hash checked;
- app data preserved across update;
- installer failure does not corrupt local app state.

---

## 68. Web/PWA tests

Если PWA существует:

- new service worker/build detected;
- unsaved state not discarded silently;
- update prompt shown;
- reload moves to new version;
- offline queue preserved;
- old incompatible API produces update-required UX, not data loss.

---

## 69. Release pipeline tests

CI должен проверять:

- version uniqueness;
- artifact name uniqueness;
- expected platform matrix complete;
- hash generated after final signed/package bytes;
- metadata references existing artifacts;
- metadata signature valid;
- release cannot be promoted before artifacts verified;
- PR pipeline cannot access production signing secrets;
- clean install smoke test;
- previous-version update smoke test.

---

## 70. Non-functional requirements

- updater не должен заметно замедлять startup;
- update check asynchronous;
- network timeout bounded;
- failed updater не ломает planner;
- current working installation остаётся runnable после pre-apply failure;
- artifacts immutable;
- all external I/O cancellable where reasonable;
- no secrets in logs;
- no elevated privilege when avoidable;
- existing offline-first guarantees preserved;
- update code separated from academic/business domain;
- typed errors;
- RU/EN UX;
- deterministic release process;
- safe production rollback/pause path.

---

## 71. Recommended implementation phases

### Phase 1 — Update domain foundation

Реализовать:

- version abstraction;
- update state machine;
- update policy model;
- channel preferences;
- platform abstraction;
- local update state store;
- unit tests.

### Phase 2 — Desktop updater

Реализовать:

- Velopack или выбранный existing proven framework adapter;
- check/download/apply/restart;
- full packages;
- update lock;
- settings UI;
- release notes;
- package verification.

### Phase 3 — Release pipeline

Реализовать:

- package generation;
- artifact hosting;
- code signing where available;
- hashes;
- signed metadata;
- stable/beta feeds;
- production promotion flow.

### Phase 4 — Rollout and compatibility

Реализовать:

- deterministic staged rollout;
- pause/withdraw;
- minimum supported version;
- independent updater endpoint;
- backend compatibility contract.

### Phase 5 — Recovery hardening

Реализовать:

- post-update health marker;
- recovery UX;
- migration rollback compatibility metadata;
- failure telemetry;
- real previous-version update E2E.

### Phase 6 — Android sideload

Если Android client существует:

- signed APK distribution;
- update check;
- verified download;
- PackageInstaller flow;
- user-action-required handling;
- Android-specific tests.

### Phase 7 — Web/PWA

Если Web/PWA существует:

- application version discovery;
- service worker/update-ready UX;
- safe reload;
- compatibility handling.

---

## 72. Acceptance criteria

### Scenario A — обычное desktop обновление

1. Пользователь использует `1.4.0`.
2. `1.5.0` публикуется в stable.
3. Клиент обнаруживает release без блокировки startup.
4. Пользователь видит краткие release notes.
5. Update скачивается.
6. Integrity validation проходит.
7. Пользователь выбирает restart/update.
8. Приложение запускается как `1.5.0`.
9. Personal/offline data сохранены.

### Scenario B — background update

1. Auto-check включён.
2. Пользователь продолжает работу.
3. Update скачивается в фоне.
4. Приложение не закрывается самостоятельно.
5. Пользователь получает `ready to install`.
6. Update применяется при выбранном restart.

### Scenario C — corrupted release

1. Artifact bytes не соответствуют expected hash/signature metadata.
2. Client отклоняет artifact.
3. Apply не начинается.
4. Current installation продолжает запускаться.
5. Typed diagnostic записан.

### Scenario D — bad rollout

1. Версия `1.5.0` доступна 5% cohort.
2. Обнаруживается проблема.
3. Release переводится в `PAUSED`.
4. Новые клиенты перестают её получать.
5. Уже установившиеся не downgrade-ятся автоматически.
6. Выпускается `1.5.1`.

### Scenario E — old unsupported client

1. Server больше не может безопасно обслуживать `1.1.0`.
2. Online operation получает machine-readable unsupported-client response.
3. Local planner/data остаются доступны.
4. Update endpoint доступен независимо.
5. Пользователь может обновиться до supported version.

### Scenario F — Android sideload

1. Пользователь установил APK не из Google Play.
2. Новая версия обнаружена приложением.
3. APK скачан и verified.
4. Android запрашивает confirmation, если этого требует ОС.
5. После подтверждения пакет обновляется поверх текущего.
6. User data сохранены.

---

## 73. Definition of Done

Фича считается production-capable для конкретной платформы, когда:

- существует canonical `IAppUpdateService` или архитектурный эквивалент;
- update check не блокирует startup;
- stable/beta разделены;
- version comparison typed;
- artifacts immutable;
- downloads verified;
- platform code signing используется там, где требуется/доступно;
- metadata authenticity проверяется;
- anti-rollback/freshness policy реализована;
- updater не хранит privileged distribution credentials;
- background download не закрывает приложение;
- apply требует безопасный lifecycle transition;
- user data не хранятся в заменяемом application directory;
- migrations имеют recovery semantics;
- staged rollout работает deterministically;
- release можно pause/withdraw;
- broken update не уничтожает working installation;
- manual recovery path существует;
- release pipeline публикует discovery metadata только после artifacts;
- предыдущая production-like версия реально обновлена до новой в E2E test;
- все critical failure paths покрыты integration tests.

---

## 74. Архитектурные инварианты

### Инвариант 1

```text
RELEASE HOSTING stores bytes.
UPDATE TRUST decides whether those bytes are allowed to run.
```

Hosting provider не является trust root.

### Инвариант 2

```text
UPDATE replaces application code,
never user-owned data.
```

### Инвариант 3

```text
A failed update must leave the last known-good installation usable
whenever failure happened before the irreversible platform boundary.
```

### Инвариант 4

```text
Forward fix is the default rollback strategy.
Binary downgrade is exceptional and compatibility-gated.
```

### Инвариант 5

```text
The updater must remain reachable when the main application API
can no longer serve the installed client version.
```

---

## 75. Практическое решение для Student Execution OS сейчас

Для текущего этапа без магазинов приложений рекомендован следующий стек:

```text
Desktop application
        │
        ▼
project-owned IAppUpdateService
        │
        ▼
signed UpdatePolicy  ← canonical target/eligibility owner
        │
        ▼
exact target version
        │
        ▼
Velopack platform adapter
        │
        ▼
package feed + immutable artifacts
        │
        ▼
GitHub Releases / public static hosting (initially)

Release control:
- stable/beta channels
- staged rollout
- pause/withdraw
- minimum-supported policy
- signed artifact size/hash

Velopack feed:
- transport/package index only
- must not independently choose a different target
```

Для Android без Google Play:

```text
same signed release policy
        +
verified APK download
        +
Android PackageInstaller
        +
OS user confirmation when required
```

Для Web/PWA:

```text
atomic deploy
+ version discovery
+ service-worker update-ready flow
+ safe user-triggered reload
```

Такой подход не привязывает Student Execution OS к Google Play или GitHub навсегда, не требует писать собственный installer engine для desktop и оставляет один общий application-level контракт обновлений.

---

## 76. Research basis / implementation references

При реализации сверять актуальную документацию выбранных platform frameworks и ОС. На момент составления ТЗ релевантны:

- Velopack documentation — cross-platform desktop installation/update, channels, delta/full packages, update sources and code signing: `https://docs.velopack.io/`
- GitHub Releases / Release Assets API — initial artifact hosting: `https://docs.github.com/en/rest/releases`
- Android `PackageInstaller` API — sideload/update installation flows and user-action requirements: `https://developer.android.com/reference/android/content/pm/PackageInstaller`
- The Update Framework (TUF) — threat model and stronger metadata-security model for future hardening: `https://theupdateframework.io/docs/`

Эти внешние системы являются implementation dependencies/reference implementations, а не владельцами Student Execution OS product semantics.
