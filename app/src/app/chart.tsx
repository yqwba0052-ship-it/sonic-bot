import { useFocusEffect } from 'expo-router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { CandleChart, type ChartData, type ChartTrade } from '@/components/candle-chart';
import { Body, Card, colors, Row, Screen } from '@/components/ui';
import { useBot } from '@/lib/bot-context';
import { coinName, EXIT_REASONS, signed, when } from '@/lib/text';

type Source = 'live' | 'replay';
type Candle = [number, number, number, number, number];
type SimTrade = ChartTrade & {
  symbol: string;
  r: number | null;
  exit_reason: string | null;
  plan: string | null;
  half_ms: number | null;
};
type Sim = {
  bars: Candle[];
  fast: number[];
  slow: number[];
  trades: SimTrade[];
  forming: Candle | null;
  now_ms: number;
  period: string | null;
  lead: number; // candles before the period, already on screen when playback starts
};
type Event = { at: number; kind: 'open' | 'half' | 'close'; trade: SimTrade };

const BAR_MS = 4 * 3600 * 1000;
const VIEW = 90; // candles on screen at once (15 days)
const TICK_MS = 150;
const SPEEDS = [
  { label: 'بطيء', step: 1 },
  { label: 'متوسط', step: 3 },
  { label: 'سريع', step: 10 },
];

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

function Control({ label, onPress, disabled }: { label: string; onPress: () => void; disabled?: boolean }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={onPress}
      style={[styles.control, disabled && { opacity: 0.4 }]}>
      <Text style={styles.controlText}>{label}</Text>
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

/** The trade as it looked at time `at`: its outcome stays hidden until it happens. */
function asOf(trade: SimTrade, at: number): SimTrade {
  if (trade.closed_ms !== null && trade.closed_ms < at) return trade;
  return {
    ...trade,
    closed_ms: null,
    exit_price: null,
    pnl: null,
    r: null,
    exit_reason: null,
    scaled: trade.half_ms !== null && trade.half_ms < at ? 1 : 0,
  };
}

function TradeDetails({ trade, at, last }: { trade: SimTrade; at: number; last: number | null }) {
  const open = trade.closed_ms === null;
  const days = ((trade.closed_ms ?? at) - trade.opened_ms) / 86_400_000;
  const move = open && last ? ((last - trade.entry_price) / trade.entry_price) * 100 : null;
  return (
    <Card tone={open ? colors.warn : (trade.pnl ?? 0) >= 0 ? colors.gain : colors.loss}>
      <Row
        label={`${trade.side === 'BUY' ? 'شراء' : 'بيع'} ${coinName(trade.symbol)}`}
        value={open ? 'مفتوحة' : `${signed(trade.pnl)}  ${(trade.r ?? 0).toFixed(2)}R`}
      />
      <Row label="الدخول" value={`${price(trade.entry_price)}  (${when(trade.opened_ms)})`} />
      {move !== null ? (
        <Row
          label="السعر الآن"
          value={`${price(last)}  (${move >= 0 ? '+' : ''}${move.toFixed(2)}%)`}
          color={move >= 0 ? colors.gain : colors.loss}
        />
      ) : null}
      <Row label="الوقف" value={price(trade.scaled ? trade.entry_price : trade.stop_price)} />
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

function years(period: string | null): number[] {
  const match = period?.match(/^(\d{4})-\d{2}\.\.(\d{4})-\d{2}$/);
  if (!match) return [];
  const list = [];
  for (let y = Number(match[1]); y <= Number(match[2]); y += 1) list.push(y);
  return list;
}

function eventText(event: Event): string {
  if (event.kind === 'open') return `▲ دخول عند ${price(event.trade.entry_price)}`;
  if (event.kind === 'half') return '✓ أخذ نصف الربح';
  return `▼ خروج ${signed(event.trade.pnl)} ${EXIT_REASONS[event.trade.exit_reason ?? ''] ?? ''}`;
}

function eventColor(event: Event): string | undefined {
  if (event.kind === 'close') return (event.trade.pnl ?? 0) > 0 ? colors.gain : colors.loss;
  return event.kind === 'half' ? colors.gain : undefined;
}

export default function SimulatorScreen() {
  const { status, call, connection } = useBot();
  const symbols = status?.limits.symbols ?? [];
  const [symbol, setSymbol] = useState<string | null>(null);
  const [source, setSource] = useState<Source>('live');
  const [months, setMonths] = useState(1);
  const [year, setYear] = useState<number | null>(null);
  const [half, setHalf] = useState<1 | 2>(1);
  const [sim, setSim] = useState<Sim | null>(null);
  const [pos, setPos] = useState(0); // closed candles revealed so far
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [chosen, setChosen] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [period, setPeriod] = useState<string | null>(null);
  const length = useRef(0);
  const active = symbol ?? symbols[0] ?? null;
  const replayYears = years(period);
  const pickedYear = year ?? replayYears[replayYears.length - 1] ?? null;

  const replayEnd = pickedYear ? `&end_ms=${Date.UTC(pickedYear, half === 1 ? 6 : 12, 1)}` : '';
  const query = !active
    ? null
    : source === 'live'
      ? `/simulate?symbol=${active}&source=live&months=${months}`
      : `/simulate?symbol=${active}&source=replay&months=6${replayEnd}`;

  useFocusEffect(
    useCallback(() => {
      if (!connection || !query) return;
      let cancelled = false;
      const live = query.includes('source=live');
      const load = (first: boolean) =>
        call<Sim>(query, 'GET')
          .then((data) => {
            if (cancelled) return;
            setError('');
            if (data.period) setPeriod(data.period);
            setSim(data);
            const before = length.current;
            length.current = data.bars.length;
            if (first) {
              setPlaying(false);
              setChosen(null);
              // Live opens at "now"; a past period opens at its start, ready to play.
              setPos(live ? data.bars.length : Math.max(1, data.lead));
            } else {
              // Keep following "now" when the viewer was there.
              setPos((p) => (p >= before ? data.bars.length : p));
            }
          })
          .catch(() => !cancelled && setError('تعذر جلب الشموع.'));
      load(true);
      const timer = live ? setInterval(() => load(false), 60_000) : undefined;
      return () => {
        cancelled = true;
        if (timer) clearInterval(timer);
      };
    }, [query, call, connection])
  );

  const total = sim?.bars.length ?? 0;
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(() => {
      setPos((p) => {
        const next = Math.min(total, p + speed);
        if (next >= total) setPlaying(false);
        return next;
      });
    }, TICK_MS);
    return () => clearInterval(timer);
  }, [playing, speed, total]);

  const atNow = source === 'live' && pos >= total;
  const view = useMemo(() => {
    if (!sim || total === 0) return null;
    const end = Math.max(1, Math.min(pos, total));
    const start = Math.max(0, end - VIEW);
    const bars = sim.bars.slice(start, end);
    const fast = sim.fast.slice(start, end);
    const slow = sim.slow.slice(start, end);
    const showForming = atNow && sim.forming !== null;
    if (showForming && sim.forming) {
      bars.push(sim.forming);
      fast.push(0);
      slow.push(0);
    }
    const at = showForming ? sim.now_ms : sim.bars[end - 1][0] + BAR_MS;
    const first = bars[0][0];
    const seen = sim.trades.filter((t) => t.opened_ms < at).map((t) => asOf(t, at));
    const onScreen = seen.filter((t) => (t.closed_ms ?? Infinity) >= first);
    const events: Event[] = [];
    for (const trade of seen) {
      events.push({ at: trade.opened_ms, kind: 'open', trade });
      if (trade.scaled && trade.half_ms !== null) events.push({ at: trade.half_ms, kind: 'half', trade });
      if (trade.closed_ms !== null) events.push({ at: trade.closed_ms, kind: 'close', trade });
    }
    events.sort((a, b) => b.at - a.at);
    const periodStart = sim.bars[Math.min(sim.lead, total - 1)][0];
    const closed = seen.filter((t) => t.closed_ms !== null && t.closed_ms >= periodStart);
    const wins = closed.filter((t) => (t.pnl ?? 0) > 0).length;
    const last = bars[bars.length - 1][4];
    const chart: ChartData = { bars, fast, slow, trades: onScreen };
    return { chart, at, seen, events, closed, wins, last, showForming };
  }, [sim, pos, total, atNow]);

  const nextEvent = () => {
    if (!sim || !view) return;
    const times = sim.trades
      .flatMap((t) => [t.opened_ms, t.half_ms, t.closed_ms])
      .filter((t): t is number => t !== null && t >= view.at)
      .sort((a, b) => a - b);
    setPlaying(false);
    if (times.length === 0) {
      setPos(total);
      return;
    }
    const index = sim.bars.findIndex((bar) => bar[0] + BAR_MS > times[0]);
    setPos(index < 0 ? total : index + 1);
  };

  let selected: SimTrade | undefined;
  if (view) {
    const latest = view.events[0]?.trade.id;
    selected =
      view.seen.find((t) => t.id === chosen) ??
      view.seen.find((t) => t.closed_ms === null) ??
      view.seen.find((t) => t.id === latest);
  }
  const pickSource = (next: Source) => {
    setSource(next);
    setPlaying(false);
  };
  const winRate = view && view.closed.length ? (view.wins / view.closed.length) * 100 : null;
  const pnl = view ? view.closed.reduce((sum, t) => sum + (t.pnl ?? 0), 0) : 0;

  return (
    <Screen title="محاكي الشموع">
      <Card>
        <View style={styles.row}>
          <Chip label="تداول البوت الآن" selected={source === 'live'} onPress={() => pickSource('live')} />
          <Chip label="صفقات الاختبار" selected={source === 'replay'} onPress={() => pickSource('replay')} />
        </View>
        <View style={styles.row}>
          {symbols.map((s) => (
            <Chip key={s} label={coinName(s)} selected={s === active} onPress={() => setSymbol(s)} />
          ))}
        </View>
        {source === 'live' ? (
          <View style={styles.row}>
            {[1, 3, 6].map((m) => (
              <Chip key={m} label={m === 1 ? 'آخر شهر' : `آخر ${m} أشهر`} selected={months === m}
                onPress={() => setMonths(m)} />
            ))}
          </View>
        ) : (
          <>
            <View style={styles.row}>
              {replayYears.map((y) => (
                <Chip key={y} label={String(y)} selected={y === pickedYear} onPress={() => setYear(y)} />
              ))}
            </View>
            <View style={styles.row}>
              <Chip label="يناير – يونيو" selected={half === 1} onPress={() => setHalf(1)} />
              <Chip label="يوليو – ديسمبر" selected={half === 2} onPress={() => setHalf(2)} />
            </View>
          </>
        )}
      </Card>

      {error ? <Body color={colors.loss}>{error}</Body> : null}

      {view ? (
        <Card>
          <View style={styles.between}>
            <Text style={styles.clock}>{when(view.showForming ? view.at : view.at - BAR_MS)}</Text>
            <Text style={[styles.clock, { color: atNow ? colors.gain : colors.muted }]}>
              {atNow ? '● مباشر' : `شمعة ${Math.max(0, Math.min(pos, total) - (sim?.lead ?? 0))} من ${total - (sim?.lead ?? 0)}`}
            </Text>
          </View>
          <CandleChart data={view.chart} focus={selected ? selected.id : null} />
          <View style={styles.controls}>
            <Control label="⏮" onPress={() => { setPlaying(false); setPos(Math.max(1, sim?.lead ?? 1)); }} />
            <Control label="◀" onPress={() => { setPlaying(false); setPos((p) => Math.max(1, p - 1)); }} />
            <Control
              label={playing ? '⏸ إيقاف' : '▶ تشغيل'}
              disabled={pos >= total}
              onPress={() => setPlaying((p) => !p)}
            />
            <Control label="▶|" disabled={pos >= total}
              onPress={() => { setPlaying(false); setPos((p) => Math.min(total, p + 1)); }} />
            <Control label="الحدث التالي ⏭" disabled={pos >= total} onPress={nextEvent} />
            {source === 'live' ? (
              <Control label="مباشر" disabled={atNow} onPress={() => { setPlaying(false); setPos(total); }} />
            ) : null}
          </View>
          <View style={styles.row}>
            {SPEEDS.map((s) => (
              <Chip key={s.step} label={s.label} selected={speed === s.step} onPress={() => setSpeed(s.step)} />
            ))}
          </View>
          <Body color={colors.muted}>
            الأصفر EMA 20 والأزرق EMA 100: البوت يشتري عندما يقطع الأصفر الأزرق للأعلى و BTC صاعد.
            ▲ دخول، ▼ خروج، والأحمر للوقف، والأخضر لهدف النصف.
            {source === 'live' ? ' في وضع المباشر آخر شمعة ما زالت تتكوّن وتتحدث كل دقيقة.' : ''}
          </Body>
        </Card>
      ) : null}

      {view ? (
        <Card>
          <Row label="صفقات انتهت حتى هذه اللحظة" value={String(view.closed.length)} />
          <Row
            label="الرابحة"
            value={winRate === null ? '—' : `${view.wins} (${winRate.toFixed(0)}%)`}
            color={winRate !== null && winRate >= 80 ? colors.gain : undefined}
          />
          <Row label="الربح والخسارة" value={signed(pnl)} color={pnl >= 0 ? colors.gain : colors.loss} />
          <Row label="مفتوحة الآن" value={String(view.seen.filter((t) => t.closed_ms === null).length)} />
        </Card>
      ) : null}

      {selected && view ? <TradeDetails trade={selected} at={view.at} last={view.last} /> : null}

      {view ? (
        <Card>
          <Body>ما فعله البوت (الأحدث أولاً):</Body>
          {view.events.length === 0 ? (
            <Body color={colors.muted}>
              {source === 'live'
                ? `لم يفتح البوت صفقة على ${active ? coinName(active) : ''} في هذه الفترة بعد. ينتظر تقاطع المتوسطين.`
                : 'لا صفقات حتى هذه اللحظة. اضغط تشغيل أو الحدث التالي.'}
            </Body>
          ) : null}
          {view.events.slice(0, 10).map((event) => (
            <Pressable
              key={`${event.trade.id}-${event.kind}`}
              accessibilityRole="button"
              onPress={() => setChosen(event.trade.id)}
              style={[styles.eventRow, chosen === event.trade.id && styles.chipOn]}>
              <Row label={when(event.at)} value={eventText(event)} color={eventColor(event)} />
            </Pressable>
          ))}
        </Card>
      ) : null}
    </Screen>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  between: { flexDirection: 'row', justifyContent: 'space-between', marginBottom: 6 },
  clock: { color: colors.text, fontSize: 13, fontWeight: '600' },
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
  controls: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 10, marginBottom: 6, direction: 'ltr' },
  control: {
    borderRadius: 8,
    backgroundColor: colors.line,
    paddingHorizontal: 14,
    paddingVertical: 10,
    minWidth: 44,
    alignItems: 'center',
  },
  controlText: { color: colors.text, fontSize: 15, fontWeight: '700' },
  eventRow: { borderRadius: 8, borderWidth: 1, borderColor: 'transparent', padding: 6 },
});
