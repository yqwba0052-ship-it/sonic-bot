const MESSAGES: Record<string, string> = {
  offline: 'لا يوجد اتصال بالخادم. قد يكون البوت ما زال يعمل.',
  invalid_trader: 'اكتب اسمًا، أو رابط حساب المتداول في Binance، أو معرّفه (أحرف وأرقام).',
  unknown_trader: 'هذا المتداول غير موجود في قائمتك.',
  no_trades: 'لا توجد صفقات لهذا المتداول بعد. استورد سجل صفقاته أولًا.',
  confirm_required: 'الحذف يحتاج تأكيدك.',
  key_not_read_only: 'رُفض المفتاح: فيه صلاحية غير القراءة (تداول أو تحويل أو سحب). أنشئ مفتاحًا بصلاحية القراءة فقط.',
  key_check_failed: 'تعذر التحقق من المفتاح عند Binance. تأكد من نسخه كاملًا ومن أن الخادم في منطقة مسموحة.',
  no_account_key: 'اربط حسابك بمفتاح قراءة فقط أولًا.',
  too_large: 'الملف كبير جدًا.',
  bad_token: 'رمز الدخول غير صحيح.',
  not_connected: 'أدخل بيانات الخادم أولًا.',
  stop_first: 'اضغط «أوقف الصفقات الجديدة» في تبويب الحالة أولًا، ثم غيّر الحدود. الصفقات المفتوحة تبقى بوقفها.',
  halted: 'البوت موقوف للأمان. راجع السبب ثم أعد التفعيل.',
  invalid_values: 'بعض القيم خارج الحدود المسموحة.',
  daily_loss_above_capital: 'الخسارة اليومية لازم تكون أقل من المبلغ أو تساويه.',
  unknown_symbol: 'عملة غير متاحة.',
  coin_below_minimum: 'مبلغك لا يكفي لأقل صفقة في عملة مختارة. ألغِ اختيارها أو ارفع المبلغ.',
  budget_used: 'خسر البوت المبلغ المخصص كله، فأقفل الصفقات وتوقف. باقي رصيدك لم يُلمس.',
  budget_still_used: 'المبلغ المخصص انتهى. احفظ مبلغًا جديدًا من تبويب الأمان أولًا.',
  daily_loss_still_breached: 'ما زالت خسارة اليوم فوق الحد. انتظر إلى اليوم التالي.',
  panic_button: 'أوقفته أنت بزر الإغلاق الطارئ.',
  daily_loss_limit: 'وصل البوت إلى حد الخسارة اليومي، فأقفل الصفقات وتوقف.',
  protection_failed: 'فشل وضع وقف الخسارة، فأقفل البوت الصفقة وتوقف.',
  unknown_position: 'توجد صفقة لا يعرفها البوت. راجعها قبل إعادة التفعيل.',
  repeated_failures: 'انقطع الاتصال بالسوق 30 دقيقة متواصلة، فتوقف البوت عن فتح صفقات جديدة. الصفقات المفتوحة باقية بوقفها.',
  connection_restored: 'عاد الاتصال بالسوق',
  started: 'بدأ البوت',
  account_linked: 'رُبط حسابك بمفتاح قراءة فقط',
  account_unlinked: 'فُك ربط حسابك',
  account_synced: 'مزامنة صفقات حسابك',
  lab_failed: 'تعذر تحديث التداول الوهمي للمتداولين (Sonic لم يتأثر)',
  stopped: 'توقف البوت',
  halt_cleared: 'أعيد تفعيل البوت',
  limits_changed: 'تغيرت حدود الأمان',
  budget_restarted: 'بدأ حساب جديد للمبلغ',
  trade_opened: 'فُتحت صفقة',
  trade_closed: 'أُقفلت صفقة',
  trade_skipped: 'لم تُفتح صفقة',
  tick_failed: 'فشلت دورة فحص',
  trade_exit_signal: 'أُقفلت صفقة لأن الاتجاه انعكس',
  trade_exit_time: 'أُقفلت صفقة بعد 40 يومًا',
  keep_awake_on: 'تفعيل منع النوم',
  auto_resume_on: 'تفعيل الاستئناف بعد إعادة التشغيل',
  auto_resume_off: 'إيقاف الاستئناف بعد إعادة التشغيل',
  resumed_after_restart: 'رجع الخادم بعد إعادة تشغيل، وأكمل البوت التداول',
  keep_awake_off: 'إيقاف منع النوم',
  trade_scaled: 'أُخذ ربح نصف الصفقة، والوقف صار عند سعر الدخول',
  strategy_drift:
    'نتائج آخر 30 صفقة أسوأ من أي فترة في 6 سنوات من الاختبار. توقف البوت عن فتح صفقات جديدة، والصفقات المفتوحة باقية بوقفها. راجع النتائج قبل إعادة التفعيل.',
};

export const EXIT_REASONS: Record<string, string> = {
  stop_loss: 'وقف الخسارة',
  breakeven: 'أُخذ نصف الربح ثم أُقفل الباقي عند سعر الدخول',
  trade_exit_signal: 'انعكس الاتجاه',
  trade_exit_time: 'مرت 40 يومًا',
  panic_button: 'إغلاق طارئ منك',
  daily_loss_limit: 'حد الخسارة اليومي',
  budget_used: 'انتهى المبلغ',
  protection_failed: 'تعذر وضع الوقف',
};

export function coinName(symbol: string): string {
  return symbol.replace(/USDT$/, '');
}

export function message(code: string): string {
  return MESSAGES[code] ?? code;
}

export function money(value: number | null | undefined): string {
  return `${(value ?? 0).toFixed(2)} USDT`;
}

export function signed(value: number | null | undefined): string {
  const amount = value ?? 0;
  return `${amount > 0 ? '+' : ''}${amount.toFixed(2)} USDT`;
}

export function when(ms: number): string {
  return new Date(ms).toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'short' });
}
