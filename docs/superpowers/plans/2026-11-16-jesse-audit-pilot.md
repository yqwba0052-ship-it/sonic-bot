# خطة تنفيذ: التجربة الصغيرة لـ Jesse مدققًا لـ Sonic

> **للمنفّذ:** مهارة مطلوبة: superpowers:executing-plans (أو subagent-driven-development). الخطوات بصيغة `- [ ]`.

**الهدف:** إثبات أن Jesse المجاني يعمل دون قاعدة بيانات أو شبكة، ثم مقارنة صفقات Sonic صفقةً بصفقة بين محرّكنا و Jesse، مع فصل أثر المحرّك عن أثر بيانات الدقيقة.

**البنية:** حزمة بحث جديدة `research/sonic_research/jesse_audit/` تستدعي دوال Sonic نفسها (قراءة فقط) داخل استراتيجية Jesse. ثلاث تشغيلات: R0 محرّكنا، R1 Jesse على دقائق مصنوعة من شموع الساعة، R2 Jesse على دقائق حقيقية. أداة مقارنة تصنّف كل فرق.

**التقنيات:** Python 3.13، Jesse 3.6.1 (MIT) ووحدة `jesse.research` فقط، numpy، بيانات data.binance.vision.

**التصميم:** `docs/superpowers/specs/2026-11-16-jesse-audit-design.md`

## قيود عامة (تنطبق على كل مهمة)

- لا تعديل لأي ملف في `server/` أو `app/` أو `deploy/` أو `.github/`؛ ولا للخادم؛ ولا لمفاتيح Binance و Kopyon.
- Jesse 3.6.1 مثبَّت الإصدار؛ لا `LICENSE_API_TOKEN`، لا إضافة التداول الحي، لا Benchmark، لا PostgreSQL.
- أي تاريخ ≥ `2024-01-01` يُرفض في كل الكود (`HOLDOUT_START_MS = 1704067200000`).
- البيئة: `%USERPROFILE%\tools\jesse-venv`، تثبيت بـ `--no-cache-dir`. البيانات: `research/data/klines_1m/`.
- الإضافة الكلية على القرص ≤ 1 GB (`DISK_BUDGET_BYTES = 1_000_000_000`)؛ تُقاس قبل كل مهمة وبعدها.
- كل commit ينتهي سطر موضوعه بـ `[skip ci]`؛ الفرع `research/jesse` فقط؛ لا PR ولا دمج.
- ثوابت المقارنة: `SLIPPAGE = 0.0002`، `FEE_RATE = 0.0005` (من `sonic.exchange`)، `PRICE_TOL = 0.00005`.
- اختبارات البحث الحالية يجب أن تبقى ناجحة دون Jesse: كل اختبار يحتاج Jesse يبدأ بـ `pytest.importorskip("jesse")`.

## نقاط مراجعة لا تغطيها مهمة واحدة

1. شمعة ساعة مسطحة (o=h=l=c) ⇒ الدقائق المصنوعة كلها بالسعر نفسه، دون قسمة على صفر. (اختبار في المهمة 2)
2. شهر ناقص في بيانات الدقيقة من Binance (404) ⇒ خطأ واضح يذكر الرمز والشهر، لا صمت. (المهمة 3)
3. صفقة مفتوحة في نهاية النافذة ⇒ تُستبعد من الطرفين وتُذكر في التقرير كـ "مفتوحة عند النهاية". (المهمة 6)
4. صفقة تبيع النصف وتخرج بالتعادل في الساعة نفسها ⇒ تُصنَّف `breakeven_same_hour` لا "غير مفسَّر". (المهمة 6)
5. فشل اتصال شبكي داخل Jesse أثناء التشغيل ⇒ يتوقف التشغيل برسالة "network_blocked: <host>" لا بنتائج ناقصة. (المهمة 1)

---

### المهمة 1: البيئة والتشغيل دون شبكة أو قاعدة بيانات

**الملفات:**
- إنشاء: `research/sonic_research/jesse_audit/__init__.py`، `research/sonic_research/jesse_audit/offline.py`
- اختبار: `research/tests/test_jesse_audit.py`

**الواجهات:**
- تُنتج: `block_network() -> ContextManager` (يرفع `NetworkBlocked("network_blocked: <host>")` عند أي `socket.connect`/`getaddrinfo`)؛ `disk_free_bytes(path="C:/") -> int`؛ `DISK_BUDGET_BYTES`.

- [ ] **الخطوة 1:** سجّل المساحة الحرة على C في `research/reports/jesse_pilot_disk.log` (سطر لكل قياس: الوقت، الخطوة، البايتات).
- [ ] **الخطوة 2:** قِس الحجم دون تثبيت: `python -m pip install --dry-run --no-cache-dir --report %TEMP%\jesse_report.json jesse==3.6.1`، واجمع أحجام الحزم من التقرير. **إن تجاوز المجموع 700 MB** (تُترك 300 MB للبيانات) ⇒ توقف وأبلغ المالك.
- [ ] **الخطوة 3:** أنشئ `%USERPROFILE%\tools\jesse-venv` وثبّت `jesse==3.6.1 pytest` بـ `--no-cache-dir`، ثم ثبّت حزمة البحث نفسها بـ `pip install -e research --no-deps`. سجّل المساحة.
- [ ] **الخطوة 4: اختبار فاشل** `test_block_network_refuses_connect`: داخل `block_network()` يرفع `socket.create_connection(("example.com", 80))` الاستثناء `NetworkBlocked` ونصّه يبدأ بـ `network_blocked:`؛ وبعد الخروج يعود السلوك الطبيعي (لا يُختبر اتصال حقيقي، يُفحص فقط أن `socket.socket.connect` أُعيد كما كان).
- [ ] **الخطوة 5:** شغّله وتأكد أنه يفشل، ثم نفّذ `offline.py`، ثم شغّله وتأكد أنه ينجح.
- [ ] **الخطوة 6: اختبار** `test_jesse_backtest_runs_offline` (`importorskip("jesse")`): داخل `block_network()`، `jesse.research.backtest` على 3 أيام من دقائق `candles_from_close_prices` واستراتيجية تشتري مرة وتبيع مرة ⇒ ينتهي دون استثناء و `result["metrics"]["total"] == 1`. هذا هو إثبات شرط المالك 4 (لا PostgreSQL مثبت على الجهاز).
- [ ] **الخطوة 7:** شغّل اختبارات البحث كاملة بالبيئة الجديدة وبالبيئة القديمة: كلها ناجحة. ثم `python -m ruff check research`.
- [ ] **الخطوة 8: Commit** `research: offline guard and Jesse environment check [skip ci]`

### المهمة 2: صنع الدقائق من شموع الساعة

**الملفات:** إنشاء `research/sonic_research/jesse_audit/synth.py`؛ اختبار في الملف نفسه أعلاه.

**الواجهات:**
- تُنتج: `minutes_from_hours(t, o, h, low, c) -> np.ndarray` بشكل `(60*N, 6)` وأعمدة Jesse `[t_ms, open, close, high, low, volume]`؛ `aggregate(minutes, minutes_per_bar) -> np.ndarray` (نفس الأعمدة)؛ `guard_training(ms) -> None` يرفع `ValueError("holdout_locked")` إذا `ms >= HOLDOUT_START_MS`.

- [ ] **الخطوة 1: اختبارات فاشلة:**
  - `test_round_trip_hour_and_4h`: لشموع ساعة عشوائية (بذرة 7، 48 شمعة) `aggregate(m, 60)` يساوي المدخل تمامًا (`np.array_equal` على o,h,l,c)، و `aggregate(m, 240)` يساوي تجميع 4 ساعات المحسوب مباشرة.
  - `test_low_before_high`: لكل ساعة، فهرس أول دقيقة تلمس الأدنى < فهرس أول دقيقة تلمس الأعلى.
  - `test_flat_bar`: o=h=l=c=5.0 ⇒ كل القيم 5.0 ولا `nan`.
  - `test_holdout_refused`: `minutes_from_hours` بوقت `1704067200000` يرفع `holdout_locked`.
- [ ] **الخطوة 2:** شغّلها وتأكد أنها تفشل.
- [ ] **الخطوة 3:** نفّذ `synth.py`: كل ساعة 3 مقاطع × 20 دقيقة (افتتاح←أدنى، أدنى←أعلى، أعلى←إغلاق)، أسعار المقطع خطية؛ افتتاح كل دقيقة = إغلاق السابقة داخل الساعة؛ high/low لكل دقيقة = max/min لافتتاحها وإغلاقها؛ الحجم = 0.
- [ ] **الخطوة 4:** شغّلها وتأكد أنها تنجح؛ ruff.
- [ ] **الخطوة 5: Commit** `research: synthetic minutes from hourly bars [skip ci]`

### المهمة 3: تحميل بيانات الدقيقة الحقيقية

**الملفات:** إنشاء `research/sonic_research/jesse_audit/data_1m.py`.

**الواجهات:**
- تستهلك: `month_range` و `BASE` من `sonic_research.data`؛ `guard_training`.
- تُنتج: `load_minutes(symbol: str, months: list[str]) -> np.ndarray` (أعمدة Jesse) مع حفظ `npy` لكل شهر في `research/data/klines_1m/`؛ `MissingMonth(symbol, month)`.

- [ ] **الخطوة 1: اختبارات فاشلة** (دون شبكة، بملف zip مصنوع في `tmp_path`): `test_parses_binance_zip_to_jesse_columns` (ترتيب الأعمدة `[t,o,c,h,l,v]`، صف الرأس يُتجاهل)، `test_missing_month_raises` (الجالب يعيد `None` ⇒ `MissingMonth` ونصّه يحوي الرمز والشهر)، `test_holdout_month_refused` (`"2024-01"` ⇒ `holdout_locked` قبل أي تحميل).
- [ ] **الخطوة 2:** فشل ← تنفيذ (المسار `{BASE}/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM}.zip`؛ الجالب قابل للحقن للاختبار) ← نجاح؛ ruff.
- [ ] **الخطوة 3: Commit** `research: 1-minute Binance loader, training months only [skip ci]`

### المهمة 4: تثبيت العملتين والنافذة قبل تشغيل Jesse

**الملفات:**
- تعديل: `research/sonic_research/engine_replay.py` — إضافة مفتاح `"halts"` إلى نتيجة `replay()`: قائمة `(code, at_ms)` لأحداث `budget_used` و `daily_loss_limit` و `strategy_drift` و `repeated_failures` من `store.events`. لا تغيير آخر.
- إنشاء: `research/sonic_research/jesse_audit/choose.py`؛ ناتج: `research/reports/jesse_pilot_choice.json`.

**الواجهات:**
- تُنتج: `pick_window(trades: list[dict], halts: list[tuple[str,int]], symbols: list[str], months: list[str]) -> dict` يعيد `{"coins": [A, B], "start": "YYYY-MM", "end": "YYYY-MM", "trades": n}`.
  القاعدة: لكل زوج عملتين ولكل نافذة 6 أشهر متتالية داخل `2021-12..2023-12` (لتبقى 6 أشهر تهيئة بعد 2021-06): عدد الصفقات المغلقة للزوج التي فُتحت وأُغلقت داخل النافذة؛ تُستبعد كل نافذة فيها أي إيقاف أمان؛ الأكبر عددًا يفوز، والتعادل للأقدم ثم للترتيب الأبجدي.

- [ ] **الخطوة 1: اختبارات فاشلة:** `test_pick_most_trades`، `test_window_with_halt_excluded`، `test_tie_breaks_earliest_then_alphabetical`، `test_never_beyond_2023_12`، و `test_replay_reports_halts` (تشغيل `replay` موجود في اختبارات البحث، يتحقق أن المفتاح `halts` قائمة).
- [ ] **الخطوة 2:** فشل ← تنفيذ ← نجاح؛ ruff؛ كل اختبارات البحث.
- [ ] **الخطوة 3:** شغّل R0 الكامل `replay("2021-06", "2023-12", Limits())` (بيانات 1h/4h الموجودة، لا تحميل جديد) ثم `pick_window`، واحفظ الاختيار مع `sha256` في `jesse_pilot_choice.json`. **من هنا الاختيار ثابت.**
- [ ] **الخطوة 4: Commit** `research: freeze the Jesse pilot coins and window [skip ci]`

### المهمة 5: استراتيجية الجسر داخل Jesse

**الملفات:** إنشاء `research/sonic_research/jesse_audit/bridge.py`؛ وملف `research/reports/jesse_filters.json` (من `engine_replay.live_filters` العامة، يُحفظ مرة واحدة).

**الواجهات:**
- تستهلك (قراءة فقط): `sonic.strategy.{Bar, entry_signal, btc_uptrend, exit_due, stop_distance, HALF_AT_R, MAX_HOLD_BARS, HISTORY_BARS}`، `sonic.risk.{Limits, Filters, size_position, stop_price, floor_to}`.
- تُنتج: `class SonicBridge(jesse.strategies.Strategy)`؛ `Ledger` مشترك بين العملتين: `realized: float`، `open_margin: dict[str,float]`، `trades: list[dict]` (كل صفقة: `symbol, opened_ms, entry, qty, stop, target, half_ms, half_price, closed_ms, exit, reason, fees, pnl`)؛ `reset_ledger(limits: Limits, filters: dict[str, Filters]) -> None`.

السلوك المطلوب (مطابق لمحرّكنا، القسم 7.4 في التصميم):
- عند كل شمعة 4 ساعات: آخر `HISTORY_BARS` شمعة للعملة و BTC تُحوَّل إلى `Bar` وتُعطى لـ `entry_signal(bars, btc_uptrend(btc))`.
- الكمية: `size_position(limits, price, stop_distance(bars), open_margin_others, filters, 30 + realized)`؛ الإيقاف `stop_price(...)`؛ النصف `floor_to(qty/2, step)` عند `entry + HALF_AT_R * stop_dist` مقرّبًا بـ `tick_size`، وإلا لا هدف.
- بعد تنفيذ النصف: الإيقاف إلى سعر الدخول. الخروج بـ `exit_due` أو بعد `MAX_HOLD_BARS` شمعة: أمر سوق.
- إعداد Jesse: `'Binance Perpetual Futures'`، `type: futures`، رافعة 3، `fee: 0.0005`، رصيد 10000 (كما في محرّكنا الورقي، المقاس من `30 + realized` لا من رصيد Jesse).

- [ ] **الخطوة 1: اختبارات فاشلة** (`importorskip("jesse")`، على دقائق مصنوعة من سلسلة صاعدة معروفة):
  - `test_bridge_enters_where_sonic_signals`: وقت أول دخول = إغلاق أول شمعة 4 ساعات تعطي فيها `entry_signal` قيمة `"BUY"` محسوبة مباشرة على البيانات نفسها.
  - `test_bridge_qty_equals_size_position`: الكمية المسجلة = ناتج `size_position` لنفس المدخلات.
  - `test_bridge_moves_stop_to_entry_after_half`: بعد تنفيذ النصف، خروج لاحق عند سعر الدخول سببه `breakeven`.
  - `test_bridge_time_exit_after_240_bars`.
- [ ] **الخطوة 2:** فشل ← تنفيذ ← نجاح؛ ruff.
- [ ] **الخطوة 3: Commit** `research: Jesse bridge calling Sonic's own rules [skip ci]`

### المهمة 6: المقارنة صفقة بصفقة

**الملفات:** إنشاء `research/sonic_research/jesse_audit/compare.py`.

**الواجهات:**
- تُنتج: `Row` (dataclass: `symbol, opened_ms, fields: dict[str, tuple[a, b, diff]], category: str, note: str`)؛ `match(left: list[dict], right: list[dict], end_ms: int) -> tuple[list[Row], list[dict], list[dict], list[dict]]` (المطابقة، غير الموجودة يمينًا، غير الموجودة يسارًا، المفتوحة عند النهاية)؛ `classify(row: Row) -> str`؛ `summary(rows) -> dict` (عدد كل فئة، مجموع فرق الربح لكل فئة، عدد غير المفسَّر).
- الفئات بالضبط: `identical`، `slippage`، `rounding`، `breakeven_same_hour`، `gap_fill`، `intrahour_order`، `missing`، `unexplained`.

- [ ] **الخطوة 1: اختبارات فاشلة** بصفقات مصنوعة يدويًا:
  - `test_slippage_only`: دخول يمين = يسار ÷ (1+0.0002) ⇒ `slippage`.
  - `test_breakeven_same_hour`: يسار يخرج بسعر إغلاق الساعة، يمين بسعر الدخول، نفس الساعة ⇒ `breakeven_same_hour`.
  - `test_gap_fill`: يسار يخرج بسعر الافتتاح تحت الإيقاف ⇒ `gap_fill`.
  - `test_intrahour_order`: يسار خرج بالإيقاف، يمين باع النصف أولًا في الساعة نفسها ⇒ `intrahour_order`.
  - `test_unexplained_when_qty_differs_beyond_step`.
  - `test_open_at_end_excluded`.
  - `test_summary_counts_and_pnl_by_category`.
- [ ] **الخطوة 2:** فشل ← تنفيذ ← نجاح؛ ruff.
- [ ] **الخطوة 3: Commit** `research: trade-by-trade comparison with fixed causes [skip ci]`

### المهمة 7: التشغيل والتقرير

**الملفات:** إنشاء `research/sonic_research/jesse_audit/run_pilot.py`؛ ناتج `research/reports/jesse_pilot.json` و `research/reports/jesse_pilot.md`.

**الواجهات:** تستهلك كل ما سبق. CLI: `python -m sonic_research.jesse_audit.run_pilot` (يرفض العمل دون `jesse_pilot_choice.json` أو إن تغيّر `sha256` الخاص به).

- [ ] **الخطوة 1:** R0: `replay(start, end, Limits(symbols=[A, B]))` بنافذة الاختيار.
- [ ] **الخطوة 2:** حمّل دقائق A و B و BTC للنافذة + 6 أشهر تهيئة (المهمة 3). سجّل المساحة؛ أوقف إن تجاوزت الإضافة `DISK_BUDGET_BYTES`.
- [ ] **الخطوة 3:** R1 (دقائق مصنوعة من 1h) و R2 (دقائق حقيقية) داخل `block_network()`.
- [ ] **الخطوة 4:** `match`/`summary` لـ R0↔R1 و R1↔R2. لكل `unexplained` في R0↔R1: افحص يدويًا واكتب السبب في `note`؛ إن ظهر سبب جديد متكرر ⇒ توقف وأبلغ المالك قبل إضافته كفئة.
- [ ] **الخطوة 5:** فحص تشغيل فقط: `monte_carlo_trades(..., num_scenarios=200, cpu_cores=4)` على R2 داخل `block_network()` ⇒ يكتمل؛ يُذكر في التقرير أنه "تشغيل للتحقق فقط".
- [ ] **الخطوة 6:** اكتب `jesse_pilot.json` (كل الصفوف، الملخصات، أوقات التشغيل، سجل المساحة، إصدارات الحزم) و `jesse_pilot.md` بالعربية، ومعه تقدير مساحة ووقت التجربة الكاملة (كل العملات، 2021-06..2026-10) محسوبًا من المقاس فعلًا.
- [ ] **الخطوة 7: التحقق قبل الإعلان:** كل اختبارات البحث (البيئتان)، ruff، `git diff --name-only main` لا يظهر فيه `server/` ولا `app/` ولا `deploy/` ولا `.github/`، ولا تاريخ ≥ 2024 في أي ملف بيانات محمَّل.
- [ ] **الخطوة 8: Commit و push للفرع فقط** `research: Jesse pilot results [skip ci]` ثم `git push -u origin research/jesse`. لا PR.
- [ ] **الخطوة 9:** اعرض على المالك: النتيجة، الاختلافات وأسبابها، أثر بيانات الدقيقة، المساحة المستعملة، وما تحتاجه التجربة الكاملة — وانتظر موافقته.

---

## تعديلات المالك على الخطة (2026-11-16)

- **المهمة 3 تضيف:** `compare_to_bars(minutes: np.ndarray, bars: Bars, minutes_per_bar: int) -> dict` بمفاتيح
  `bars, missing_bars, missing_minutes, mismatches` (قائمة `(t_ms, field, ours, binance_1m)`)، `max_rel_diff`.
  اختبارات: `test_compare_identical_bars`، `test_compare_reports_missing_minute`، `test_compare_reports_price_mismatch`.
- **المهمة 5 تضيف:** الجسر يحسب `entry_eff = raw * (1 + SLIPPAGE)` ثم الإيقاف والهدف منه (كمحرّكنا)، ويسجّل لكل جزء
  السعر الخام والكمية والوقت (`legs`).
- **المهمة 6 تضيف:** `costs(legs: list[dict]) -> dict` (انزلاق + رسوم بطريقة محرّكنا) تُطبَّق على الطرفين؛
  `Row` يحمل `execution_diff` و `cost_diff` منفصلين. محرّكنا: السعر الخام = سعر التنفيذ ÷ (1 ± 0.0002).
  اختبار: `test_cost_diff_separated_from_execution`.
- **المهمة 7 تضيف قبل خطوة R1↔R2:** `compare_to_bars` لكل رمز على 1h و 4h، والنتيجة في التقرير.
  R1 يوصف في التقرير بأنه "اختبار توافق المحرّكين فقط".

---

## المرحلة الثانية (موافقة المالك 2026-11-16): إصلاحات قبل تجربة التدريب الكاملة

شروط المالك: لا تعديل لـ Sonic أو الخادم أو التطبيق أو المفاتيح؛ بيانات التجربة الكبيرة على USB ولا تحميل قبل
تأكيده؛ لا 2024–2026؛ الفرع `research/jesse` فقط؛ لا دمج ولا نشر ولا APK.

- **المهمة 8 — ترتيب الشمعة وحد المراكز:** `jesse_audit/planner.py`:
  `plan_bar(limits, filters, realized, open_trades, exits, candidates) -> BarPlan(entries, skipped, projected_realized)`
  يحاكي `Engine._on_bar`: كل الخروجات أولًا (ربحها المتوقع يدخل الميزانية وهامشها يتحرر)، ثم الدخول بترتيب
  `limits.symbols` حتى `max_open`. الجسر يستدعيه مرة واحدة لكل شمعة 4 ساعات (في `before()`)، ويسجّل أي فرق
  بين الربح المتوقع والفعلي. اختبارات: الخروج قبل الدخول، الحد الأقصى، تخطّي المبلغ الصغير لا يُحسب، تحرير الهامش،
  وتكامل Jesse بعملتين و `max_open=1`.
- **المهمة 9 — دقة المقارنة:** هامش على مقياس "التك" (M2)؛ ربط `breakeven_same_hour` بإغلاق الساعة و `gap_fill`
  بافتتاحها (M1)؛ صفقة مفتوحة في طرف ومغلقة في الآخر = "مفتوحة عند النهاية" (M4).
- **المهمة 10 — تثبيت وأمان:** `replay(..., filters=None)` يقبل قيودًا مثبّتة وتعيد الصفقات المفتوحة (M3, M4)؛
  حظر `connect_ex` و `sendto` (M5)؛ تصفير دفتر الجسر قبل مونت كارلو (M6).
- **المهمة 11 — وضع التدريب الكامل:** `run_pilot --full --data-dir <USB>`: العملات ذات البيانات الكاملة من
  `Limits().symbols`، النافذة 2021-06..2023-12 والتهيئة 2020-12..2021-05؛ قياس الحجم مسبقًا بطلبات HEAD دون تحميل؛
  تقرير: المتطابق، كل اختلاف وسببه، وأثر دقة الدقيقة على الربح والخسارة.
- **المهمة 12 — إعادة التجربة الصغيرة** بعد الإصلاحات (البيانات الموجودة على C، لا تحميل جديد).
- **المهمة 13 — مقترح منفصل لتصنيف "النصف ثم التعادل":** وثيقة + اختبار + رقعة (patch) في `docs/proposals/`،
  تُجرَّب في نسخة عمل مؤقتة ثم تُحذف؛ لا تغيير في `server/` في أي فرع.
