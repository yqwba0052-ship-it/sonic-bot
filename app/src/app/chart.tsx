import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { CandleChart, type ChartData, type ChartTrade } from '@/components/candle-chart';
import { Body, Card, colors, Row, Screen } from '@/components/ui';
import { useBot } from '@/lib/bot-context';
import { coinName, EXIT_REASONS, signed, when } from '@/lib/text';

type Source = 'live' | 'replay';
type FullTrade = ChartTrade & {
  symbol: string;
  r: number | null;
  exit_reason: string | null;
  plan: string | null;
};
type Chart = Omit<ChartData, 'trades'> & { trades: FullTrade[] };
type ReplayList = { period: string | null; trades: FullTrade[] };

function Chip({ label, selected, onPress }: { label: string; selected: boolean; onPress: () => void }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      onPress={onPress}
      style={[styles.chip, selected && styles.chipOn]}>
      <Text style={styles.chipText}>{label}</Text>
    </Pressable>
  );
}

function price(value: number | null): string {
  return value === null ? '—' : String(Number(value.toPrecision(6)));
}

function why(plan: string | null): string {
  if (!plan) return '';
  try {
    const p = JSON.parse(plan) as { fast?: number; slow?: number; btc_up?: boolean; stop_pct?: number };
    if (p.fast === undefined) return '';
    return `EMA20 ${p.fast.toPrecision(5)} فوق EMA100 ${p.slow?.toPrecision(5)}، و BTC ${p.btc_up ? 'صاعد' : 'هابط'}، والوقف على بعد ${((p.stop_pct ?? 0) * 100).toFixed(1)}%`;
  } catch {
    return '';
  }
}

function TradeDetails({ trade, nowMs }: { trade: FullTrade; nowMs: number }) {
  const open = trade.closed_ms === null;
  const days = ((trade.closed_ms ?? nowMs) - trade.opened_ms) / 86_400_000;
  return (
    <Card tone={open ? colors.warn : (trade.pnl ?? 0) >= 0 ? colors.gain : colors.loss}>
      <Row
        label={`${trade.side === 'BUY' ? 'شراء' : 'بيع'} ${coinName(trade.symbol)}`}
        value={open ? 'مفتوحة الآن' : `${signed(trade.pnl)}  ${(trade.r ?? 0).toFixed(2)}R`}
      />
      <Row label="الدخول" value={`${price(trade.entry_price)}  (${when(trade.opened_ms)})`} />
      <Row label="الوقف" value={price(trade.stop_price)} />
      {trade.target_price ? <Row label="هدف النصف" value={price(trade.target_price)} /> : null}
      {trade.closed_ms ? (
        <Row label="الخروج" value={`${price(trade.exit_price)}  (${when(trade.closed_ms)})`} />
      ) : null}
      <Row label="المدة" value={`${days.toFixed(1)} يوم`} />
      {trade.scaled ? <Body color={colors.gain}>✓ أُخذ ربح النصف، والوقف انتقل لسعر الدخول</Body> : null}
      {trade.exit_reason ? (
        <Row label="سبب الإقفال" value={EXIT_REASONS[trade.exit_reason] ?? trade.exit_reason} />
      ) : null}
      {why(trade.plan) ? <Body color={colors.muted}>سبب الدخول: {why(trade.plan)}</Body> : null}
    </Card>
  );
}

export default function ChartScreen() {
  const { status, call, connection } = useBot();
  const symbols = status?.limits.symbols ?? [];
  const [symbol, setSymbol] = useState<string | null>(null);
  const [source, setSource] = useState<Source>('live');
  const [focus, setFocus] = useState<number | null>(null);
  const [chart, setChart] = useState<Chart | null>(null);
  const [replay, setReplay] = useState<ReplayList | null>(null);
  const [error, setError] = useState('');
  const active = symbol ?? symbols[0] ?? null;

  useFocusEffect(
    useCallback(() => {
      if (!connection || !active) return;
      let cancelled = false;
      const load = () => {
        const params = `symbol=${active}&source=${source}${focus ? `&trade_id=${focus}` : ''}`;
        call<Chart>(`/chart?${params}`, 'GET')
          .then((data) => !cancelled && (setChart(data), setError('')))
          .catch(() => !cancelled && setError('تعذر جلب الشموع.'));
        if (source === 'replay') {
          call<ReplayList>(`/replay/trades?symbol=${active}`, 'GET')
            .then((data) => !cancelled && setReplay(data))
            .catch(() => undefined);
        }
      };
      load();
      const timer = source === 'live' ? setInterval(load, 60_000) : undefined;
      return () => {
        cancelled = true;
        if (timer) clearInterval(timer);
      };
    }, [active, source, focus, call, connection])
  );

  const shown = chart?.trades ?? [];
  const selected = shown.find((t) => t.id === focus) ?? shown.find((t) => t.closed_ms === null);

  return (
    <Screen title="الشموع">
      <Card>
        <View style={styles.row}>
          <Chip label="التداول الحالي" selected={source === 'live'} onPress={() => { setSource('live'); setFocus(null); }} />
          <Chip label="صفقات الاختبار" selected={source === 'replay'} onPress={() => { setSource('replay'); setFocus(null); }} />
        </View>
        <View style={styles.row}>
          {symbols.map((s) => (
            <Chip key={s} label={coinName(s)} selected={s === active} onPress={() => { setSymbol(s); setFocus(null); }} />
          ))}
        </View>
        <Body color={colors.muted}>
          شموع 4 ساعات. البوت يشتري عندما يقطع الأصفر (EMA 20) الأزرق (EMA 100) للأعلى و BTC صاعد.
          ▲ دخول، ▼ خروج، والخطوط: أحمر للوقف، وأخضر لهدف النصف، ورمادي لسعر الدخول.
        </Body>
      </Card>
      {error ? <Body color={colors.loss}>{error}</Body> : null}
      {chart ? (
        <Card>
          <CandleChart data={chart} focus={focus} />
        </Card>
      ) : null}
      {selected && chart ? (
        <TradeDetails
          trade={selected}
          nowMs={(chart.bars[chart.bars.length - 1]?.[0] ?? selected.opened_ms) + 4 * 3600 * 1000}
        />
      ) : null}
      {source === 'live' && shown.length === 0 ? (
        <Card>
          <Body>لا صفقات على {active ? coinName(active) : ''} في هذه الفترة.</Body>
        </Card>
      ) : null}
      {source === 'replay' ? (
        <Card>
          <Body>
            صفقات الاختبار على الأسعار الماضية {replay?.period ? `(${replay.period})` : ''}. اضغط صفقة
            لرؤيتها على الشموع.
          </Body>
          {(replay?.trades ?? []).length === 0 ? (
            <Body color={colors.muted}>لا توجد صفقات اختبار لهذه العملة.</Body>
          ) : null}
          {(replay?.trades ?? []).slice().reverse().map((trade) => (
            <Pressable
              key={trade.id}
              accessibilityRole="button"
              onPress={() => setFocus(trade.id)}
              style={[styles.tradeRow, focus === trade.id && styles.chipOn]}>
              <Row
                label={when(trade.opened_ms)}
                value={`${signed(trade.pnl)}  ${EXIT_REASONS[trade.exit_reason ?? ''] ?? ''}`}
                color={(trade.pnl ?? 0) >= 0 ? colors.gain : colors.loss}
              />
            </Pressable>
          ))}
        </Card>
      ) : null}
    </Screen>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: {
    borderRadius: 8,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.background,
    paddingHorizontal: 12,
    paddingVertical: 8,
  },
  chipOn: { borderColor: colors.gain, backgroundColor: '#12291D' },
  chipText: { color: colors.text, fontSize: 14, fontWeight: '600' },
  tradeRow: { borderRadius: 8, borderWidth: 1, borderColor: 'transparent', padding: 6 },
});
