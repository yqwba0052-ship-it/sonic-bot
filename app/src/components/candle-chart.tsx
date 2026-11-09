import { useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { colors } from '@/components/ui';

export type ChartTrade = {
  id: number;
  side: 'BUY' | 'SELL';
  entry_price: number;
  stop_price: number;
  target_price: number;
  opened_ms: number;
  closed_ms: number | null;
  exit_price: number | null;
  pnl: number | null;
  scaled: number | null;
};

export type ChartData = {
  bars: [number, number, number, number, number][];
  fast: number[];
  slow: number[];
  trades: ChartTrade[];
};

const BAR_MS = 4 * 3600 * 1000;
const HEIGHT = 300;
const FAST_COLOR = '#F2B441';
const SLOW_COLOR = '#3B82F6';

function dateLabel(ms: number): string {
  return new Date(ms).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
}

/** Candles (time runs left to right), the two EMAs as dots, and each trade's entry, exit,
 * stop and half-profit lines. Plain views, so it works on phones and in the browser. */
export function CandleChart({ data, focus }: { data: ChartData; focus: number | null }) {
  const [width, setWidth] = useState(0);
  const bars = data.bars;
  if (bars.length === 0) return null;
  const first = bars[0][0];
  const levels = data.trades.flatMap((t) =>
    [t.entry_price, t.stop_price, t.target_price || t.entry_price, t.exit_price ?? t.entry_price]
  );
  const high = Math.max(...bars.map((b) => b[2]), ...levels);
  const low = Math.min(...bars.map((b) => b[3]), ...levels);
  const span = high - low || 1;
  const y = (price: number) => ((high - price) / span) * (HEIGHT - 16) + 8;
  const step = width / bars.length;
  const x = (ms: number) => ((ms - first) / BAR_MS + 0.5) * step;
  const end = bars[bars.length - 1][0] + BAR_MS;

  return (
    <View style={styles.ltr}>
      <View
        style={[styles.frame, { height: HEIGHT }]}
        onLayout={(event) => setWidth(event.nativeEvent.layout.width)}>
        {width > 0 &&
          bars.map(([t, o, h, l, c], index) => {
            const up = c >= o;
            const color = up ? colors.gain : colors.loss;
            const left = index * step;
            const body = Math.max(1, step * 0.6);
            return (
              <View key={t}>
                <View
                  style={[styles.abs, {
                    left: left + step / 2 - 0.5, top: y(h), width: 1,
                    height: Math.max(1, y(l) - y(h)), backgroundColor: color,
                  }]}
                />
                <View
                  style={[styles.abs, {
                    left: left + (step - body) / 2, top: y(Math.max(o, c)), width: body,
                    height: Math.max(1, Math.abs(y(o) - y(c))), backgroundColor: color,
                  }]}
                />
                {data.fast[index] ? (
                  <View style={[styles.dot, { left: left + step / 2 - 1, top: y(data.fast[index]) - 1,
                    backgroundColor: FAST_COLOR }]} />
                ) : null}
                {data.slow[index] ? (
                  <View style={[styles.dot, { left: left + step / 2 - 1, top: y(data.slow[index]) - 1,
                    backgroundColor: SLOW_COLOR }]} />
                ) : null}
              </View>
            );
          })}
        {width > 0 &&
          data.trades.map((trade) => {
            const from = Math.max(0, x(trade.opened_ms));
            const to = Math.min(width, x(trade.closed_ms ?? end));
            const lineWidth = Math.max(2, to - from);
            const strong = focus === null || focus === trade.id;
            const result = (trade.pnl ?? 0) >= 0 ? colors.gain : colors.loss;
            return (
              <View key={trade.id} style={{ opacity: strong ? 1 : 0.35 }}>
                <View style={[styles.line, { left: from, width: lineWidth, top: y(trade.stop_price),
                  backgroundColor: colors.loss }]} />
                {trade.target_price ? (
                  <View style={[styles.line, { left: from, width: lineWidth,
                    top: y(trade.target_price), backgroundColor: colors.gain }]} />
                ) : null}
                <View style={[styles.line, { left: from, width: lineWidth, top: y(trade.entry_price),
                  backgroundColor: colors.muted }]} />
                <Text style={[styles.marker, { left: from - 6, top: y(trade.entry_price) + 2,
                  color: colors.gain }]}>▲</Text>
                {trade.closed_ms && trade.exit_price ? (
                  <Text style={[styles.marker, { left: to - 6, top: y(trade.exit_price) - 16,
                    color: result }]}>▼</Text>
                ) : null}
              </View>
            );
          })}
      </View>
      <View style={styles.axis}>
        <Text style={styles.axisText}>{dateLabel(first)}</Text>
        <Text style={styles.axisText}>{dateLabel(bars[Math.floor(bars.length / 2)][0])}</Text>
        <Text style={styles.axisText}>{dateLabel(bars[bars.length - 1][0])}</Text>
      </View>
      <View style={styles.legend}>
        <Legend color={FAST_COLOR} label="EMA 20" />
        <Legend color={SLOW_COLOR} label="EMA 100" />
        <Legend color={colors.loss} label="الوقف" />
        <Legend color={colors.gain} label="هدف النصف" />
        <Legend color={colors.muted} label="الدخول" />
      </View>
    </View>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <View style={styles.legendItem}>
      <View style={[styles.swatch, { backgroundColor: color }]} />
      <Text style={styles.axisText}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  ltr: { direction: 'ltr' },
  frame: {
    backgroundColor: colors.background,
    borderColor: colors.line,
    borderWidth: 1,
    borderRadius: 8,
    overflow: 'hidden',
  },
  abs: { position: 'absolute' },
  dot: { position: 'absolute', width: 2, height: 2, borderRadius: 1 },
  line: { position: 'absolute', height: 1, opacity: 0.9 },
  marker: { position: 'absolute', fontSize: 12, fontWeight: '700' },
  axis: { flexDirection: 'row', justifyContent: 'space-between', marginTop: 4 },
  axisText: { color: colors.muted, fontSize: 11 },
  legend: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginTop: 6 },
  legendItem: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  swatch: { width: 10, height: 3, borderRadius: 1 },
});
