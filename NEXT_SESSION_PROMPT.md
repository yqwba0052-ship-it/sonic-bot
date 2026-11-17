# برومبت الجلسة القادمة

انسخ النص التالي والصقه في Claude عند العودة:

---

أنا مالك مشروع Sonic Bot. نكمل من حيث توقفنا. قبل أي عمل:

1. افتح المشروع `C:\Users\sa\Projects\sonic_bot`، وانتقل إلى الفرع `research/classical-strategies` (أحدث عمل؛ قبله
   `research/hummingbot` ثم `research/freqai` ثم `research/jesse`)، واسحب آخر نسخة من GitHub (`git fetch` ثم `git status`).
   لا تغيّر الفرع main.
2. اقرأ كاملًا:
   - `SESSION_HANDOFF.md` (ابدأ بالقسم 13، ثم 12، ثم 11، ثم 10ج، ثم 10ب).
   - `research/reports/classical_strategies_final.md` و `research/reports/hummingbot_final.md`
     و `research/reports/freqai_final.md` و `research/reports/jesse_holdout.md`.
   - `docs/STORAGE_PLAN.md`.
   - `D:\Sonic-Classical-Research\FINAL_SESSION_STATUS.md`.
3. تحقق دون أي تغيير:
   - `git log -1`، وأن الفرع مطابق لـ `origin/research/classical-strategies`، وأن الشجرة نظيفة.
   - أن الفلاشة موصولة، وأن مجلدات `D:\Sonic-*` موجودة. إن تغيّر حرفها فأخبرني.
   - مساحة C و D.
   - تشغيل الاختبارات: الأوامر في الأقسام 5 و11 و12 و13 من `SESSION_HANDOFF.md`.
   - حالة Sonic على الخادم بقراءة فقط (`/health`). لا توقفه ولا تغيّره.
4. اعرض عليّ بالعربية ملخصًا قصيرًا: نتائج المراحل الأربع (Jesse مُعتمد مدققًا، FreqAI مرفوض، Hummingbot مرفوض،
   الاستراتيجيات الكلاسيكية: لا استراتيجية مؤهلة)، وما ينتظر قراري.
   **ثم انتظر قراري، ولا تبدأ أي تطوير.**

قواعد ثابتة:
- لا تعدّل Sonic الأساسي أو الخادم أو التطبيق أو المفاتيح.
- لا تفعّل التداول الحقيقي، ولا تشترك في خدمات مدفوعة.
- اختبارات 2024–2026 (Jesse و FreqAI والتنفيذ والكلاسيكية) تمت مرة واحدة وأُقفلت: لا تُعِد تشغيلها، ولا تحذف ملفات القفل، ولا تعدّل نتائجها.
- لا تعدّل إعدادات Sonic أو Jesse أو FreqAI أو التنفيذ أو قواعد المقارنة بناءً على نتائج 2024–2026.
- FreqAI والتنفيذ البديل (Hummingbot) والاستراتيجيات الكلاسيكية رُفضت: لا تضفها إلى Sonic ولا إلى التداول الورقي دون تصميم جديد وموافقتي.
- لا تثبّت Docker أو WSL على C.
- لا تدمج في main، ولا تشغّل GitHub Actions، ولا تبنِ APK. أي commit بحثي يحمل `[skip ci]`.
- لا تطبّق المقترحات المؤجلة دون موافقتي.
- البيانات الكبيرة على الفلاشة لا على C.
- استخدم مهارات Superpowers، واعرض الخطة بالعربية قبل الكود.

---
