import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Body, Button, Card, colors, Field, Screen } from '@/components/ui';
import type { Limits, Market } from '@/lib/api';
import { errorCode, useBot } from '@/lib/bot-context';
import { coinName, message } from '@/lib/text';

type NumberKey = Exclude<keyof Limits, 'symbols'>;

const FIELDS: { key: NumberKey; label: string; hint: string }[] = [
  {
    key: 'capital_usdt',
    label: 'المبلغ الذي يتداول به البوت (USDT)',
    hint: 'لا يتجاوزه البوت. إذا خسر المبلغ كله يقفل الصفقات ويتوقف، ولا يلمس باقي رصيدك.',
  },
  {
    key: 'max_daily_loss_usdt',
    label: 'أقصى خسارة يومية (USDT)',
    hint: 'لازم تكون أقل من المبلغ أو تساويه. عند الوصول إليها يتوقف البوت لبقية اليوم.',
  },
  { key: 'leverage', label: 'الرافعة المالية', hint: 'من 1 إلى 5.' },
  { key: 'risk_per_trade_pct', label: 'المخاطرة في الصفقة (%)', hint: 'من 0.1 إلى 5.' },
  { key: 'stop_loss_pct', label: 'وقف الخسارة (%)', hint: 'من 0.2 إلى 10.' },
  { key: 'take_profit_pct', label: 'جني الربح (%)', hint: 'من 0.2 إلى 30.' },
];

function initial(limits: Limits) {
  return Object.fromEntries(FIELDS.map(({ key }) => [key, String(limits[key])])) as Record<
    NumberKey,
    string
  >;
}

function parse(values: Record<NumberKey, string>) {
  return Object.fromEntries(
    FIELDS.map(({ key }) => [key, Number(values[key].replace(',', '.'))])
  ) as Record<NumberKey, number>;
}

/** Same rule as the server: the stop risks the set share, margin stays inside the amount. */
function maxOrder(numbers: Record<NumberKey, number>): number {
  const { capital_usdt, risk_per_trade_pct, stop_loss_pct, leverage } = numbers;
  if (![capital_usdt, risk_per_trade_pct, stop_loss_pct, leverage].every(Number.isFinite)) return 0;
  if (stop_loss_pct <= 0) return 0;
  const byRisk = (capital_usdt * risk_per_trade_pct) / 100 / (stop_loss_pct / 100);
  return Math.max(0, Math.min(byRisk, capital_usdt * leverage));
}

export default function SafetyScreen() {
  const { status } = useBot();
  if (!status) {
    return (
      <Screen title="الأمان">
        <Card>
          <Body>تظهر الحدود بعد الاتصال بالخادم.</Body>
        </Card>
      </Screen>
    );
  }
  return <LimitsForm limits={status.limits} />;
}

function LimitsForm({ limits }: { limits: Limits }) {
  const { status, online, call } = useBot();
  const [values, setValues] = useState(() => initial(limits));
  const [symbols, setSymbols] = useState<string[]>(limits.symbols);
  const [markets, setMarkets] = useState<Market[] | null>(null);
  const [notice, setNotice] = useState<{ text: string; color: string } | null>(null);
  const locked = Boolean(status?.running);

  useEffect(() => {
    call<Market[]>('/markets', 'GET')
      .then(setMarkets)
      .catch(() => setMarkets([]));
  }, [call]);

  const numbers = parse(values);
  const largest = maxOrder(numbers);
  const fits = (market: Market) => market.min_order_usdt <= largest;

  const toggle = (market: Market) => {
    if (locked) return;
    setNotice(null);
    setSymbols((current) =>
      current.includes(market.symbol)
        ? current.filter((symbol) => symbol !== market.symbol)
        : [...current, market.symbol]
    );
  };

  const save = async () => {
    if (Object.values(numbers).some((value) => !Number.isFinite(value))) {
      setNotice({ text: message('invalid_values'), color: colors.loss });
      return;
    }
    if (!markets || markets.length === 0) {
      setNotice({ text: 'انتظر حتى تظهر العملات من Binance.', color: colors.loss });
      return;
    }
    if (symbols.length === 0) {
      setNotice({ text: 'اختر عملة واحدة على الأقل.', color: colors.loss });
      return;
    }
    // Recomputed here from the values being saved, not taken from an earlier render.
    const limit = maxOrder(numbers);
    const tooSmall = markets.filter(
      (market) => symbols.includes(market.symbol) && market.min_order_usdt > limit
    );
    if (tooSmall.length > 0) {
      const names = tooSmall.map((market) => coinName(market.symbol)).join('، ');
      setNotice({
        text: `مبلغك لا يكفي لـ ${names}. ألغِ اختيارها أو ارفع المبلغ.`,
        color: colors.loss,
      });
      return;
    }
    try {
      await call('/limits', 'PUT', { ...numbers, symbols });
      setNotice({ text: 'حُفظت الحدود، وبدأ حساب المبلغ من الآن.', color: colors.gain });
    } catch (failure) {
      setNotice({ text: message(errorCode(failure)), color: colors.loss });
    }
  };

  return (
    <Screen title="الأمان">
      {locked ? (
        <Card tone={colors.warn}>
          <Body color={colors.warn}>{message('stop_first')}</Body>
        </Card>
      ) : null}
      <Card>
        {FIELDS.map(({ key, label, hint }) => (
          <Field
            key={key}
            label={label}
            hint={hint}
            keyboardType="decimal-pad"
            editable={!locked}
            value={values[key]}
            onChangeText={(text) => setValues({ ...values, [key]: text })}
          />
        ))}
      </Card>
      <Card>
        <Body>العملات التي يتداول فيها البوت</Body>
        <Body color={colors.muted}>
          أكبر صفقة يسمح بها مبلغك وحدودك: {largest.toFixed(2)} USDT. العملة الرمادية أقل صفقة
          فيها أكبر من ذلك.
        </Body>
        {markets === null ? <Body color={colors.muted}>جاري جلب العملات من Binance…</Body> : null}
        {markets?.length === 0 ? (
          <Body color={colors.loss}>تعذر جلب العملات. تأكد من اتصال الخادم.</Body>
        ) : null}
        <View style={styles.coins}>
          {(markets ?? []).map((market) => {
            const selected = symbols.includes(market.symbol);
            const ok = fits(market);
            return (
              <Pressable
                key={market.symbol}
                accessibilityRole="checkbox"
                accessibilityState={{ checked: selected, disabled: locked }}
                accessibilityLabel={coinName(market.symbol)}
                onPress={() => toggle(market)}
                style={[
                  styles.coin,
                  selected && styles.selected,
                  !ok && styles.unavailable,
                  selected && !ok && styles.selectedBlocked,
                ]}>
                <Text style={[styles.coinName, !ok && styles.mutedText]}>
                  {selected ? '✓ ' : ''}
                  {coinName(market.symbol)}
                </Text>
                <Text style={[styles.coinHint, ok ? null : styles.lossText]}>
                  {ok
                    ? `أقل صفقة ${market.min_order_usdt}$`
                    : `تحتاج ${market.min_order_usdt}$`}
                </Text>
              </Pressable>
            );
          })}
        </View>
      </Card>
      {notice ? <Body color={notice.color}>{notice.text}</Body> : null}
      <Button
        label={markets === null ? 'جاري جلب العملات…' : 'احفظ الحدود'}
        disabled={!online || locked || !markets?.length}
        onPress={save}
      />
    </Screen>
  );
}

const styles = StyleSheet.create({
  coins: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  coin: {
    minWidth: 96,
    minHeight: 56,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.background,
    paddingHorizontal: 12,
    paddingVertical: 8,
    justifyContent: 'center',
  },
  selected: { borderColor: colors.gain, backgroundColor: '#12291D' },
  unavailable: { opacity: 0.55 },
  selectedBlocked: { borderColor: colors.loss, opacity: 1 },
  coinName: { color: colors.text, fontSize: 16, fontWeight: '700' },
  coinHint: { color: colors.muted, fontSize: 12, marginTop: 2 },
  mutedText: { color: colors.muted },
  lossText: { color: colors.loss },
});
