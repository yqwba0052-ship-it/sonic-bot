import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Body, Button, Card, colors, ConfirmButton, Field, Row } from '@/components/ui';

type Paper = { trades: number; needed: number; win_rate: number | null; net_pnl: number | null; passed: boolean };
export type Allocation = {
  model_id: number;
  trader_name: string;
  symbols: string[];
  leverage: number;
  amount: number;
  realized: number;
  used: number;
  available: number;
  exposure: number;
  possible_loss: number;
  max_loss: number | null;
  daily_loss: number | null;
  state: 'approved' | 'allocated' | 'live' | 'stopped';
  mode: 'demo' | 'real' | null;
  close_requested: number;
  open: { symbol: string; side: string; qty: number; entry_price: number; stop_price: number }[];
  paper: Paper;
};
export type Allocations = {
  items: Allocation[];
  trade_key: { linked: boolean; hint: string | null };
  live_allowed: boolean;
  phrase: string;
};
type Bounds = {
  min: number;
  max: number;
  wallet: number;
  available: number;
  reserved_by_others: number;
  per_coin: Record<string, number>;
};
type Gate = { key: string; ok: boolean; text: string };
type Call = <T>(path: string, method?: string, body?: unknown, timeoutMs?: number) => Promise<T>;

const STATE: Record<Allocation['state'], string> = {
  approved: 'معتمدة (بلا مبلغ، لا تتداول)',
  allocated: 'لها مبلغ (لا تتداول بعد)',
  live: 'تعمل',
  stopped: 'موقوفة',
};

function usd(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(2)} USDT`;
}

function One({
  item,
  phrase,
  liveAllowed,
  call,
  report,
}: {
  item: Allocation;
  phrase: string;
  liveAllowed: boolean;
  call: Call;
  report: (text: string, ok: boolean) => Promise<void>;
}) {
  const [bounds, setBounds] = useState<Bounds | null>(null);
  const [amount, setAmount] = useState('');
  const [maxLoss, setMaxLoss] = useState(item.max_loss ? String(item.max_loss) : '');
  const [dailyLoss, setDailyLoss] = useState(item.daily_loss ? String(item.daily_loss) : '');
  const [mode, setMode] = useState<'demo' | 'real'>('demo');
  const [gate, setGate] = useState<Gate[] | null>(null);
  const [typed, setTyped] = useState('');
  const base = `/allocations/${item.model_id}`;
  const run = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      await report(done, true);
    } catch (failure) {
      await report(failure instanceof Error ? failure.message : 'invalid_values', false);
    }
  };
  const live = item.state === 'live';
  return (
    <View style={styles.item}>
      <Row label={`${item.trader_name} — ${item.symbols.map((s) => s.replace('USDT', '')).join('، ')}`} value="" />
      <Row
        label="الحالة"
        value={`${STATE[item.state]}${live ? (item.mode === 'real' ? ' بأموال حقيقية' : ' على Binance Demo') : ''}`}
        color={live ? colors.gain : item.state === 'stopped' ? colors.muted : colors.warn}
      />
      <Row label="المخصص" value={usd(item.amount)} />
      <Row label="المستخدم في صفقات مفتوحة" value={usd(item.used)} />
      <Row label="المتاح لها" value={usd(item.available)} />
      <Row label="الربح والخسارة المحققة" value={usd(item.realized)} color={item.realized >= 0 ? colors.gain : colors.loss} />
      <Row label="التعرض (قيمة الصفقات المفتوحة)" value={usd(item.exposure)} />
      <Row label="الرافعة" value={`×${item.leverage}`} />
      <Row label="أقصى خسارة لو ضُربت كل الوقوف الآن" value={usd(item.possible_loss)} />

      <Body color={colors.muted}>
        الحجز داخلي في Sonic: Binance لا تعزل مبلغًا لكل استراتيجية داخل حساب العقود نفسه. لا تتعدى الاستراتيجية مبلغها، ولا
        يُخصص نفس الرصيد لاستراتيجيتين. أموال النسخ في حساب النسخ المنفصل لا تُلمس.
      </Body>

      <Button
        label="اعرض الحد الأدنى والأعلى"
        color={colors.line}
        onPress={() =>
          run(async () => setBounds(await call<Bounds>(`${base}/bounds`, 'GET', undefined, 60_000)), 'قُرئ الرصيد من حسابك.')
        }
      />
      {bounds ? (
        <View>
          <Row label="رصيد محفظة العقود" value={usd(bounds.wallet)} />
          <Row label="المتاح في Binance" value={usd(bounds.available)} />
          <Row label="محجوز لاستراتيجيات أخرى" value={usd(bounds.reserved_by_others)} />
          <Row label="الحد الأدنى (أصغر صفقة على كل عملاتها)" value={usd(bounds.min)} />
          <Row label="الحد الأعلى (الرصيد، ونصف المحفظة لاستراتيجية، و80% للجميع)" value={usd(bounds.max)} />
          {bounds.available <= 0 ? <Body color={colors.loss}>لا يوجد رصيد متاح.</Body> : null}
        </View>
      ) : null}
      <Field label="المبلغ المخصص (USDT)" value={amount} onChangeText={setAmount} keyboardType="decimal-pad" />
      <Button
        label={item.amount ? 'عدّل المبلغ' : 'خصص المبلغ'}
        disabled={!amount.trim()}
        onPress={() =>
          run(
            () => call(base, 'PUT', { amount: Number(amount) }, 60_000),
            'حُفظ المبلغ (حجز داخلي فقط، ولا يبدأ أي تداول).'
          )
        }
      />
      <Field label="حد الخسارة الكلي (USDT)" value={maxLoss} onChangeText={setMaxLoss} keyboardType="decimal-pad" />
      <Field label="حد الخسارة اليومي (USDT)" value={dailyLoss} onChangeText={setDailyLoss} keyboardType="decimal-pad" />
      <Button
        label="احفظ حدود الخسارة"
        color={colors.line}
        disabled={!maxLoss.trim() || !dailyLoss.trim()}
        onPress={() =>
          run(
            () => call(`${base}/limits`, 'PUT', { max_loss: Number(maxLoss), daily_loss: Number(dailyLoss) }),
            'حُفظت الحدود: عند الحد الكلي تتوقف وتُغلق صفقاتها، وعند اليومي لا تفتح جديدًا حتى اليوم التالي.'
          )
        }
      />

      {item.state !== 'stopped' ? (
        <View style={styles.box}>
          <Body>التفعيل (خطوة منفصلة)</Body>
          <View style={styles.row}>
            {(['demo', 'real'] as const).map((m) => (
              <Pressable
                key={m}
                accessibilityRole="button"
                accessibilityState={{ selected: mode === m }}
                onPress={() => {
                  setMode(m);
                  setGate(null);
                }}
                style={[styles.chip, mode === m && styles.chipOn]}>
                <Text style={styles.chipText}>{m === 'demo' ? 'Binance Demo (أولًا)' : 'أموال حقيقية'}</Text>
              </Pressable>
            ))}
          </View>
          {mode === 'real' && !liveAllowed ? (
            <Body color={colors.warn}>
              التداول الحقيقي مقفل في إعدادات الخادم. يفتحه المالك فقط بوضع SONIC_LIVE_TRADING=yes في ملف ‎.env على الخادم.
            </Body>
          ) : null}
          <Button
            label="افحص الشروط"
            color={colors.line}
            onPress={() => run(async () => setGate(await call<Gate[]>(`${base}/gate?mode=${mode}`, 'GET')), 'فُحصت الشروط.')}
          />
          {gate?.map((g) => (
            <Text key={g.key} style={[styles.line, { color: g.ok ? colors.gain : colors.loss }]}>
              {g.ok ? '✓' : '✗'} {g.text}
            </Text>
          ))}
          <Body color={colors.muted}>للموافقة اكتب الجملة كما هي: «{phrase}»</Body>
          <Field label="موافقتك" value={typed} onChangeText={setTyped} />
          <Button
            label={mode === 'real' ? 'فعّل بأموال حقيقية' : 'فعّل على Binance Demo'}
            color={mode === 'real' ? colors.loss : colors.accent}
            disabled={!typed.trim() || live}
            onPress={() =>
              run(() => call(`${base}/activate`, 'POST', { mode, phrase: typed }), 'فُعّلت. تبدأ عند إغلاق شمعة الـ4 ساعات التالية.')
            }
          />
          <Button
            label="إيقاف: لا صفقات جديدة، والمفتوحة تكمل بوقفها وهدفها"
            color={colors.line}
            onPress={() => run(() => call(`${base}/stop`, 'POST', { close_open: false }), 'أُوقفت. المفتوحة تكمل حتى وقفها أو هدفها.')}
          />
          <ConfirmButton
            label="إيقاف وإغلاق المفتوحة بسعر السوق"
            confirmLabel="اضغط مرة ثانية للتأكيد"
            color={colors.loss}
            onPress={() => run(() => call(`${base}/stop`, 'POST', { close_open: true }), 'أُوقفت، وتُغلق المفتوحة في الدورة القادمة.')}
          />
        </View>
      ) : (
        <Body color={colors.muted}>
          موقوفة{item.close_requested ? '، وتُغلق صفقاتها المفتوحة' : '، وصفقاتها المفتوحة (إن وجدت) تكمل بوقفها وهدفها'}. خصص
          مبلغًا من جديد لإرجاعها، ثم فعّلها كخطوة منفصلة.
        </Body>
      )}
      <Row
        label="اختبارها الوهمي"
        value={`${item.paper.trades} من ${item.paper.needed} صفقة، ${item.paper.passed ? 'ناجح' : 'لم يكتمل/لم ينجح'}`}
        color={item.paper.passed ? colors.gain : colors.warn}
      />
    </View>
  );
}

export function AllocationsCard({
  data,
  call,
  report,
}: {
  data: Allocations | null;
  call: Call;
  report: (text: string, ok: boolean) => Promise<void>;
}) {
  const [key, setKey] = useState('');
  const [secret, setSecret] = useState('');
  if (!data) return null;
  return (
    <Card>
      <Body>الاستراتيجيات المعتمدة ورأس المال</Body>
      <Body color={colors.muted}>
        ثلاث خطوات منفصلة: ✓ اعتماد (بلا مال)، ثم تخصيص مبلغ (حجز داخلي)، ثم تفعيل يحتاج اختبارًا وهميًا ناجحًا وموافقتك المكتوبة
        ومفتاح تداول بلا سحب ولا تحويل وحدود خسارة. Sonic الأساسي لا يتأثر.
      </Body>
      <Row
        label="التداول الحقيقي على الخادم"
        value={data.live_allowed ? 'مسموح' : 'مقفل'}
        color={data.live_allowed ? colors.loss : colors.gain}
      />
      {data.items.length === 0 ? (
        <Body color={colors.muted}>لم تعتمد أي استراتيجية بعد. اضغط ✓ بجانب استراتيجية نجحت في الاختبار.</Body>
      ) : null}
      {data.items.map((item) => (
        <One key={item.model_id} item={item} phrase={data.phrase} liveAllowed={data.live_allowed} call={call} report={report} />
      ))}
      <View style={styles.box}>
        <Body>مفتاح التداول (العقود الآجلة فقط)</Body>
        <Body color={colors.muted}>
          يُرفض أي مفتاح فيه سحب أو تحويل أو تداول فوري/هامش. يُفضّل تقييده بعنوان IP الخادم من إعدادات المفتاح في Binance.
        </Body>
        {data.trade_key.linked ? (
          <>
            <Row label="المفتاح" value={data.trade_key.hint ?? ''} />
            <Button label="احذف مفتاح التداول" color={colors.line} onPress={async () => {
              await call('/trade-key', 'DELETE');
              await report('حُذف مفتاح التداول.', true);
            }} />
          </>
        ) : (
          <>
            <Field label="API Key للتداول" value={key} onChangeText={setKey} autoCapitalize="none" />
            <Field label="Secret Key" value={secret} onChangeText={setSecret} autoCapitalize="none" secureTextEntry />
            <Button
              label="تحقق واحفظ"
              disabled={!key.trim() || !secret.trim()}
              onPress={async () => {
                try {
                  await call('/trade-key', 'PUT', { key: key.trim(), secret: secret.trim() }, 30_000);
                  setKey('');
                  setSecret('');
                  await report('حُفظ مفتاح التداول بعد التحقق من صلاحياته.', true);
                } catch (failure) {
                  await report(failure instanceof Error ? failure.message : 'invalid_values', false);
                }
              }}
            />
          </>
        )}
      </View>
      {data.items.some((i) => i.state === 'live' || i.open.length) ? (
        <ConfirmButton
          label="وقف طوارئ: أوقف كل الاستراتيجيات وأغلق صفقاتها"
          confirmLabel="اضغط مرة ثانية للتأكيد"
          color={colors.loss}
          onPress={async () => {
            await call('/allocations/emergency', 'POST', {}, 60_000);
            await report('أُوقفت كل الاستراتيجيات وأُغلقت صفقاتها. صفقاتك الأخرى لم تُلمس.', true);
          }}
        />
      ) : null}
    </Card>
  );
}

const styles = StyleSheet.create({
  item: { borderTopWidth: 1, borderColor: colors.line, paddingTop: 10, marginTop: 6, gap: 6 },
  box: { borderWidth: 1, borderColor: colors.line, borderRadius: 10, padding: 10, gap: 6, marginTop: 6 },
  row: { flexDirection: 'row', gap: 8, flexWrap: 'wrap' },
  chip: { borderRadius: 8, borderWidth: 1, borderColor: colors.line, paddingHorizontal: 12, paddingVertical: 8 },
  chipOn: { borderColor: colors.gain, backgroundColor: '#12291D' },
  chipText: { color: colors.text, fontSize: 14, fontWeight: '600' },
  line: { fontSize: 13, lineHeight: 20 },
});
