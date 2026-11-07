const MESSAGES: Record<string, string> = {
  offline: 'لا يوجد اتصال بالخادم. قد يكون البوت ما زال يعمل.',
  bad_token: 'رمز الدخول غير صحيح.',
  not_connected: 'أدخل بيانات الخادم أولًا.',
  stop_first: 'أوقف البوت أولًا، ثم غيّر الحدود.',
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
  repeated_failures: 'تكرر فشل الاتصال بالسوق، فتوقف البوت.',
  started: 'بدأ البوت',
  stopped: 'توقف البوت',
  halt_cleared: 'أعيد تفعيل البوت',
  limits_changed: 'تغيرت حدود الأمان',
  trade_opened: 'فُتحت صفقة',
  trade_closed: 'أُقفلت صفقة',
  trade_skipped: 'لم تُفتح صفقة',
  tick_failed: 'فشلت دورة فحص',
  trade_exit_signal: 'أُقفلت صفقة لأن الاتجاه انعكس',
  trade_exit_time: 'أُقفلت صفقة بعد 20 يومًا',
  strategy_drift:
    'نتائج آخر 30 صفقة أسوأ من أي فترة في 6 سنوات من الاختبار. توقف البوت عن فتح صفقات جديدة، والصفقات المفتوحة باقية بوقفها. راجع النتائج قبل إعادة التفعيل.',
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
