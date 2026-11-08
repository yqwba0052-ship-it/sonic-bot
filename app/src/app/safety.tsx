import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { Body, Button, Card, colors, Field, Screen } from '@/components/ui';
import type { Limits, Market } from '@/lib/api';
import { errorCode, useBot } from '@/lib/bot-context';
import { coinName, message } from '@/lib/text';

type NumberKey = Exclude<keyof Limits, 'symbols' | 'allow_shorts'>;

const FIELDS: { key: NumberKey; label: string; hint: string }[] = [
  {
    key: 'capital_usdt',
    label: 'المبلغ الذي يتداول به البوت (USDT)',
    hint: 'لا يتجاوزه البوت. إذا خسر المبلغ كله يقفل الصفقات ويتوقف، ولا يلمس باقي رصيدك. تغيير هذا الرقم يبدأ حسابًا جديدًا للمبلغ، وتغيير باقي الحدود لا يمسح الخسائر السابقة.',
  },
  {
    key: 'max_daily_loss_usdt',
    label: 'أقصى خسارة يومية (USDT)',
    hint: 'لازم تكون أقل من المبلغ أو تساويه. عند الوصول إليها يتوقف البوت لبقية اليوم.',
  },
  {
    key: 'leverage',
    label: 'الرافعة المالية القصوى',
    hint: 'من 1 إلى 5. الرافعة تحدد الهامش المحجوز فقط، والخسارة يحددها الوقف والمخاطرة. ×24 ممنوعة: Binance يصفي الصفقة بعد 3.7% تقريبًا، قبل الوقف.',
  },
  {
    key: 'risk_per_trade_pct',
    label: 'المخاطرة في الصفقة (%)',
    hint: 'من 0.25 إلى 2. المختبر: 1. الرقم الأعلى يكبر الأرباح والخسائر معًا.',
  },
  { key: 'max_open', label: 'أقصى صفقات مفتوحة معًا', hint: 'من 1 إلى 6. المختبر: 3.' },
];

/** Same rule as the server: a too-small trade may be raised to the minimum order only
 * while its stop risks at most max(2 x risk per trade, 2%) of the amount. */
function raiseCap(riskPct: number): number {
  return Math.max(2 * (Number.isFinite(riskPct) ? riskPct : 0), 2) / 100;
}

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

/** Same rule as the server: the smallest order's stop must fit in 2% of the amount,
 * and its margin inside the amount. */
function minAmount(market: Market, leverage: number, riskPct: number): number {
  const lev = Number.isFinite(leverage) && leverage > 0 ? leverage : 1;
  return Math.max(
    (market.min_order_usdt * market.stop_pct) / raiseCap(riskPct),
    market.min_order_usdt / lev
  );
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
  const [allowShorts, setAllowShorts] = useState<boolean>(limits.allow_shorts);
  const [markets, setMarkets] = useState<Market[] | null>(null);
  const [notice, setNotice] = useState<{ text: string; color: string } | null>(null);
  const locked = Boolean(status?.running);

  useEffect(() => {
    call<Market[]>('/markets', 'GET')
      .then(setMarkets)
      .catch(() => setMarkets([]));
  }, [call]);

  const numbers = parse(values);
  const fits = (market: Market) =>
    minAmount(market, numbers.leverage, numbers.risk_per_trade_pct) <= numbers.capital_usdt;

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
    const tooSmall = markets.filter(
      (market) =>
        symbols.includes(market.symbol) &&
        minAmount(market, numbers.leverage, numbers.risk_per_trade_pct) > numbers.capital_usdt
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
      await call('/limits', 'PUT', { ...numbers, symbols, allow_shorts: allowShorts });
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
        <Pressable
          accessibilityRole="switch"
          accessibilityState={{ checked: allowShorts, disabled: locked }}
          accessibilityLabel="السماح بصفقات البيع"
          onPress={() => !locked && setAllowShorts((value) => !value)}
          style={[styles.coin, allowShorts && styles.selectedBlocked]}>
          <Text style={styles.coinName}>
            {allowShorts ? '✓ ' : ''}السماح بصفقات البيع (Short)
          </Text>
          <Text style={styles.coinHint}>
            مطفأ وهو الموصى به: صفقات البيع خسرت في 2024–2026، وصفقات الشراء ربحت كل سنة.
          </Text>
        </Pressable>
      </Card>
      <Card>
        <Body>العملات التي يتداول فيها البوت</Body>
        <Body color={colors.muted}>
          تحت كل عملة أقل مبلغ يكفيها اليوم، حسب أقل صفقة في Binance ومسافة الوقف الحالية.
          العملة الرمادية تحتاج مبلغًا أكبر من مبلغك.
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
                    ? `يكفيها ${Math.ceil(minAmount(market, numbers.leverage, numbers.risk_per_trade_pct))}$`
                    : `تحتاج ${Math.ceil(minAmount(market, numbers.leverage, numbers.risk_per_trade_pct))}$`}
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
