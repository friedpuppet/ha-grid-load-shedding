# План: інтеграція `grid_load_shedding` — детекція міської мережі + вимкнення/відновлення навантажень

## Context
Зараз логіка «зникла мережа → вимкнути потужні розетки → повернулась → ввімкнути назад»
зібрана з розрізнених частин у HA:
- `binary_sensor.e_elektrika` — UI template helper (напруга `sensor.inverter_grid_voltage` > 170 V,
  утримання стану при `unavailable` < 30 с, далі запасний ping). Межа 30 с неточна (30–90 с),
  бо шаблон з `now()` HA перераховує раз на хвилину.
- 18 template-перемикачів `switch.<розетка>_vimikati_bez_merezhi`, прив'язаних до пристроїв розеток.
- Автоматизація `power_shed_heavy_loads` (automations.yaml).
- Trigger-template сенсор `sensor.vimkneno_cherez_vidkliuchennia` (configuration.yaml),
  який оновлюється подією `power_shed_update`.
- `input_number.zatrimka_vmikannia_pislia_poiavi_merezhi` — затримка відновлення.
- Подія `power_shed_forget`, яку надсилає `boiler_schedule` о 07:00.

7 жовтня все це коректно відпрацювало реальне відключення: 9 розеток вимкнулися, через 60 с
після повернення мережі ввімкнулися назад. Мета — перетворити це на одну узагальнену
інтеграцію з config flow, публічний репозиторій і встановлення через HACS (як
`ha-server-monitor`), без прив'язки до конкретних сутностей. Рішення користувача: публічний
репо + HACS; обсяг — детекція мережі + вимкнення.

## Назви
- Repo `ha-grid-load-shedding`, domain `grid_load_shedding`, назва «Grid Load Shedding».
- Локальна робоча копія — підпроект `~/work/claude/homeassistant/grid-load-shedding/`
  (git, власний `AGENTS.md`), як `esp-inverter/`.
- Репозиторій на GitHub створює користувач (наявний PAT обмежений двома репо
  ha-server-monitor*). Org: `ha-linux-monitoring` або особистий акаунт — уточнити при
  створенні. Код до цього пишеться й комітиться локально.

## Архітектура (HA ≥ 2025.3, тут 2026.8.3)

Одна config entry = одна «установка» (джерело мережі + її навантаження). Один пристрій
інтеграції «Grid» з сутностями нижче.

**Config flow (`async_step_user`)**: назва; сенсор напруги (entity selector, `sensor`,
device_class voltage); поріг V (170); утримання при недоступності, с (30); необов'язковий
запасний `binary_sensor`; затримка відновлення, с (60). **Options flow** — ті самі параметри.

**Навантаження — config subentries** (`ConfigSubentryFlow`, кнопка «Add load» в UI
інтеграції): вибрати `switch` (entity selector). Для кожного створюється:
- `switch` «Shed on grid loss» (увімкнено/вимкнено, `RestoreEntity`), прив'язаний до пристрою
  розетки через `homeassistant.helpers.device.async_device_info_to_link_from_entity`.
  Так само зараз роблять template-хелпери з `device_id`.

**Сутності на пристрої «Grid»**:
- `binary_sensor` **Grid** (device_class `power`), уся логіка в Python:
  - `async_track_state_change_event` на сенсор напруги й запасний сенсор;
  - напруга числова → `> поріг`;
  - напруга `unavailable`/`unknown` → тримати поточний стан і запустити `async_call_later(hold)`;
  - після закінчення таймера → запасний сенсор (або `off`, якщо запасного немає);
  - повернення числової напруги скасовує таймер;
  - старт: `RestoreEntity` + одразу перерахунок.

  Це прибирає неточність 30–90 с: таймер точний.
- `sensor` **Shed loads**: кількість вимкнених + атрибут `entities`. Список зберігається через
  `homeassistant.helpers.storage.Store`, тож переживає рестарт Core.
- `number` **Restore delay** (с, `RestoreNumber`): замість `input_number`.
- `button` **Restore now**: увімкнути збережені розетки негайно й очистити список.

**Логіка вимкнення** (координатор у `__init__.py`/`shedder.py`, підписаний на сутність Grid):
- Grid `on → off`: для кожного навантаження з увімкненим «Shed on grid loss», чий `switch`
  зараз `on` → `switch.turn_off`; додати до збереженого списку (union з наявним).
  Реагує лише на справжній перехід `on → off`, не на `unavailable` (як зараз).
- Grid `on` стабільно `Restore delay` с (`async_call_later`, скасовується, якщо мережа знову
  зникла) → `switch.turn_on` для збереженого списку, очистити.
- **Сервіс** `grid_load_shedding.forget` (`entity_id`) — прибрати розетку зі списку, щоб її не
  відновлювало. Замінює подію `power_shed_forget`.
- Події `grid_load_shedding_shed` / `_restored` (з переліком entity_id), щоб інші автоматизації
  (Telegram тощо) могли реагувати.

**Файли** (`custom_components/grid_load_shedding/`): `manifest.json` (`iot_class: calculated`,
`config_flow: true`, `integration_type: service` — спершу був `helper`, але тоді інтеграція видна лише в «Помічниках»; змінено у v0.2.1), `const.py`, `__init__.py`, `config_flow.py`
(entry + options + subentry flows), `grid.py` (логіка детекції, чиста й тестовна), `shedder.py`
(логіка вимкнення/відновлення + Store), `binary_sensor.py`, `sensor.py`, `switch.py`,
`number.py`, `button.py`, `services.yaml`, `strings.json`, `translations/{en,uk}.json`.
Плюс у корені `hacs.json` (`"homeassistant": "2025.3.0"`), `README.md`, `LICENSE`.
Зразок структури — `~/work/claude/eee-homeserver/ha-server-monitor/`.

## Тести
`tests/` з `pytest-homeassistant-custom-component` (через `uv`):
- **Grid:** поріг; утримання < hold; перехід на запасний сенсор після hold (з
  `async_fire_time_changed`); відновлення стану після рестарту.
- **Shedder:** вимикає лише ті, що `on` і з увімкненим прапорцем; відновлює через delay;
  скасування при повторному зникненні; `forget`; Store переживає reload entry.
- **Config / options / subentry flows.**

GitHub Actions: `hassfest` + HACS validation + pytest.

## Перехід на живому HA — ✅ виконано 2026-10-07 (HA 2026.9.4)
1. Бекап `automations.yaml`, `configuration.yaml`, `.storage/core.config_entries`.
2. Встановити через HACS (custom repo), створити entry: напруга
   `sensor.inverter_grid_voltage`, поріг 170, hold 30, запасний
   `binary_sensor.esp_svitlobot_pinger_local`, delay = поточне значення `input_number`.
3. Додати 18 навантажень (subentries) і перенести стан прапорців зі старих template-перемикачів
   (скриптом через WS `config_entries/subentries/...` та `switch.turn_on/off`).
4. Вимкнути автоматизацію `power_shed_heavy_loads`. Перевірити, що старий список порожній.
5. Видалити UI-хелпер «Є електрика», перейменувати нову сутність Grid на
   `binary_sensor.e_elektrika` (WS `config/entity_registry/update`), щоб Power Watchdog, бойлер і
   дашборд не змінювались.
6. Бойлер (`switch.rozetka_boiler`): у його навантаження задати вікно 01:00–07:00, а стан «Працювати
   за розкладом» узяти з `input_boolean.boiler_vikoristovuietsia`. Автоматизацію `boiler_schedule`
   видалити (вікна в інтеграції замінили її, v0.2.0). Перевірити дашборди на посилання на
   `input_boolean.boiler_vikoristovuietsia` і замінити їх новим перемикачем.
7. Прибрати старе: 18 template-перемикачів, trigger-template сенсор, `input_number`,
   `input_boolean.boiler_vikoristovuietsia`, автоматизації `power_shed_heavy_loads` і `boiler_schedule`.
8. Оновити `electricity.md` і корінь `AGENTS.md`.

## Verification
- `uv run pytest` — усі тести зелені; hassfest/HACS validation у CI.
- На живому HA:
  - `binary_sensor.e_elektrika` = `on`, джерело — нова інтеграція;
  - 18 перемикачів «Shed on grid loss» на пристроях розеток зі станами як у старих;
  - `sensor` Shed loads = 0.
- Імітація без реального відключення (через тестову entry з input_number-напругою замість
  інвертора): падіння напруги → розетки з прапорцем вимикаються, лічильник = N; повернення
  → через delay вмикаються.
- Справжній тест — наступне відключення, порівняти з логом 7 жовтня.
