# خطة التخزين: الحفاظ على مساحة قرص C

آخر تحديث: 2026-11-16. الهدف: لا يمتلئ قرص اللابتوب C. البيانات الكبيرة والنماذج والتقارير الثقيلة
توضع على فلاشة SanDisk (USB، حجمها 114.6 GB، نظام الملفات exFAT).

## الوضع الحالي (2026-11-16)
| المكان | المحتوى | الحجم |
|---|---|---|
| `C:\Users\sa\tools\jesse-venv` | بيئة Python لـ Jesse 3.6.1 | ≈ 780 MB |
| `C:\Users\sa\Projects\sonic_bot\research\data\klines` | شموع 4h/1h/30m/15m (zip) لبحوث Sonic | ≈ 148 MB |
| `C:\Users\sa\Projects\sonic_bot\research\data\klines_1m` | دقائق التجربة الصغيرة (SOL/TRX/BTC، 2022-07..2023-06) | ≈ 73 MB |
| `D:\Sonic-Jesse-Research` | دقائق التجربة الموسّعة (7 عملات + BTC، 2020-12..2023-12، `.npy`) | ≈ 620 MB عند الاكتمال |
| `D:\SteamLibrary`, `D:\Games` | ملفات المالك — **لا تُلمس** | — |

## القواعد
1. **بيئات Python تبقى على C** (`C:\Users\sa\tools\<name>-venv`): تشغيلها من فلاشة exFAT بطيء، وتتعطل إذا
   تغيّر حرف القرص. لا تُنقل بيئة أثناء أي تشغيل.
2. **كل ما يكبر مع الوقت يذهب إلى D:** البيانات التاريخية، النماذج المدرَّبة، نتائج الاختبارات الكبيرة،
   السجلات، والملفات المؤقتة الكبيرة.
3. **لا تنزيل مكرر:** مصدر واحد لبيانات Binance على الفلاشة، وكل أداة تقرأ منه أو تحوّل منه.
4. **في git فقط** الكود والتقارير الصغيرة (JSON/MD). البيانات الكبيرة لا تُرفع أبدًا.
5. لا فرمتة، ولا حذف لملفات موجودة، ولا فصل للفلاشة أثناء أي كتابة.
6. قبل أي تثبيت كبير: قِس الحجم المتوقع (`pip install --dry-run --report`) وسجّل مساحة C و D.

## البنية المقترحة على الفلاشة (لم يُنشأ منها إلا Sonic-Jesse-Research)
```
D:\
├─ Sonic-Jesse-Research\          ← موجود: دقائق Jesse (.npy)، لا تتغير بنيته
├─ Sonic-Market-Data\             ← مقترح: مصدر بيانات Binance المشترك
│  └─ binance-um\monthly\klines\<SYMBOL>\<interval>\<SYMBOL>-<interval>-<YYYY-MM>.zip
│     (نفس مسار data.binance.vision، فتُحوَّل منه أي أداة دون تنزيل جديد)
├─ Sonic-FreqAI\                  ← مقترح (لا تثبيت الآن)
│  └─ user_data\  (data, models, backtest_results, logs) — يُشغَّل بـ --userdir / --datadir
├─ Sonic-Hummingbot\              ← مقترح (لا تثبيت الآن)
│  └─ conf\, logs\, data\
└─ Sonic-Reports\                 ← مقترح: تقارير كبيرة لا تُرفع إلى git
```

## بيانات Binance المشتركة (لتجنب التنزيل المتكرر)
- المصدر الوحيد: الملفات الشهرية العامة من data.binance.vision (مجانية).
- **الخطوة المقترحة لاحقًا** (تحتاج موافقة): جعل `sonic_research/data.py` و `jesse_audit/data_1m.py`
  يقرآن مسار البيانات من متغير بيئة (مثل `SONIC_MARKET_DATA=D:\Sonic-Market-Data`) ويحتفظان بالـ zip هناك،
  ثم التحويل إلى `.npy` لـ Jesse أو `feather` لـ FreqTrade من الملفات نفسها.
- الدقائق الموجودة على D (`.npy`) تُستعمل مباشرة بأمر `run_pilot --full --data-dir D:\Sonic-Jesse-Research`
  دون تنزيل جديد.

## FreqAI (لاحقًا، بعد موافقة المالك)
- FreqTrade مفتوح المصدر (GPL-3.0). FreqAI جزء منه.
- البيئة على C (`C:\Users\sa\tools\freqtrade-venv`). **تجنّب PyTorch** في البداية (حجمه أكثر من 2 GB):
  نماذج LightGBM أو XGBoost تكفي وحجمها مئات MB فقط.
- `user_data` كاملًا على `D:\Sonic-FreqAI`: النماذج المدرَّبة تكبر بسرعة.
- البيانات: تحويل من `D:\Sonic-Market-Data` إلى صيغة FreqTrade بدل `freqtrade download-data` المتكرر.
- القيود نفسها: تدريب 2021–2023، و2024–2026 مغلقة حتى تثبيت الإعدادات، ولا مفاتيح ولا تداول حقيقي.

## Hummingbot (لاحقًا، بعد موافقة المالك)
- Hummingbot مفتوح المصدر (Apache-2.0). يُثبَّت عادة عبر Docker أو conda، وكلاهما كبير.
  يُقاس الحجم قبل أي شيء.
- `conf/logs/data` على `D:\Sonic-Hummingbot`. **لا مفاتيح Binance حقيقية**: الوضع الورقي فقط.

## تحذير
- إذا لم تكن الفلاشة موصولة، تتوقف الأدوات التي تحتاجها (مثل `--full`) برسالة واضحة، ولا تكتب إلى C بدلًا منها.
- إذا تغيّر حرف الفلاشة (مثلًا E: بدل D:)، يُمرَّر المسار الجديد في الأمر، فلا شيء مثبَّت على الحرف داخل الكود.

## تحديث 2026-11-17: مختبر FreqAI (موجود فعلًا)
| المكان | المحتوى | الحجم |
|---|---|---|
| `D:\Sonic-FreqAI\venv` | Python 3.13 + Freqtrade 2026.10 + LightGBM/sklearn/datasieve/xgboost | 666 MB فعليًا (يحجز ≈ 2.8 GB على exFAT) |
| `D:\Sonic-FreqAI\data\binance` و `binance-holdout` | شموع 4h بصيغة feather (محوَّلة من zip الموجودة) | ≈ 4 MB |
| `D:\Sonic-FreqAI\user_data\models` | نماذج FreqAI المحفوظة لكل نافذة | ≈ 1.1 GB |
| `D:\Sonic-FreqAI\predictions` | توقعات كل تشغيل (CSV لكل عملة) | ≈ 12 MB |
| `D:\Sonic-FreqAI\reports`, `logs`, `configs`, `cache\tmp` | تقارير، فحوص، أقفال، سجلات، ملفات مؤقتة | صغيرة |
| `D:\Sonic-FreqAI\progress.json` | نقاط الاستئناف | — |
- **قاعدة مضافة:** بيئة FreqAI على الفلاشة (بطلب المالك). C لم يُستهلك منه إلا حوالي 20 MB للتثبيت. إن تغيّر حرف الفلاشة
  فالبيئة تحتاج إعادة إنشاء: `python -m venv` ثم `pip install -r D:\Sonic-FreqAI\logs\pip_freeze.txt` مع
  `TMP/TEMP` على الفلاشة و `--no-cache-dir`.
- مصدر البيانات واحد: ملفات `research/data/klines` الموجودة، تُحوَّل بـ `freqai_lab/convert.py` دون تنزيل.

## تحديث 2026-11-17: مختبر التنفيذ (Hummingbot)
| المكان | المحتوى | الحجم |
|---|---|---|
| `D:\Sonic-Hummingbot\data\aggTrades\<SYMBOL>\` | سجل صفقات Binance اليومي لأيام دخول Sonic فقط (205 ملفات zip) | ≈ 812 MB |
| `D:\Sonic-Hummingbot\reports` | نتائج كل تشغيل، والعتبة، والأحكام | < 1 MB |
| `D:\Sonic-Hummingbot\progress.json`, `FINAL_SESSION_STATUS.md` | نقاط الاستئناف وحالة الإغلاق | — |
- **لا بيئة جديدة:** المختبر يعمل ببيئة البحث المعتادة (`server\.venv`، numpy فقط). Hummingbot لم يُثبَّت، لأنه يحتاج
  Docker أو WSL2 على C.
- التنزيل يكتب ملفًا مؤقتًا `.part` ثم يعيد تسميته، فالملف المنقطع لا يُعتبر مكتملًا.

## تحديث 2026-11-17: مختبر الاستراتيجيات الكلاسيكية
- `D:\Sonic-Classical-Research`: نسخ النتائج (`reports`)، والسجلات (`logs`)، والتقدم (`progress.json`)، وحالة الإغلاق. الحجم صغير
  (أقل من 5 MB).
- لا بيئة جديدة ولا بيانات جديدة: المختبر يعمل ببيئة البحث المعتادة (`server\.venv`، numpy فقط) على شموع 4h الموجودة في
  `research/data/klines`.
