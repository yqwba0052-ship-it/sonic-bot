import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';

import { Body, Card, colors, Row, Screen } from '@/components/ui';
import type { BotEvent, Insights, Trade } from '@/lib/api';
import { useBot } from '@/lib/bot-context';
import { coinName, message, signed, when } from '@/lib/text';

function rText(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}R`;
}

function rColor(value: number | null | undefined) {
  if (value === null || value === undefined) return undefined;
  return value >= 0 ? colors.gain : colors.loss;
}

function Learned({ insights }: { insights: Insights | null }) {
  if (!insights) return null;
  const need = insights.window - insights.recent_trades;
  return (
    <Card>
      <Body>ماذا تعلّم البوت من صفقاته</Body>
      <Body color={colors.muted}>
        النتائج هنا بوحدة المخاطرة: الرقم 1 يعني ربحًا بقدر ما خاطر به البوت، والرقم 1- يعني
        خسارة بقدره. المتوقع من الاختبار حوالي {insights.expected_avg_r} في المتوسط لكل صفقة،
        مع سلاسل خسائر طبيعية.
      </Body>
      <Row
        label={`متوسط آخر ${insights.recent_trades} صفقة`}
        value={rText(insights.recent_avg_r)}
        color={rColor(insights.recent_avg_r)}
      />
      <Body color={colors.muted}>
        {need > 0
          ? `يحتاج ${need} صفقة أخرى قبل أن يحكم هل السوق تغير.`
          : `يتوقف تلقائيًا إذا نزل المتوسط تحت ${insights.drift_floor}، وهو أسوأ من أي فترة في الاختبار.`}
      </Body>
      {insights.coins.map((coin) => (
        <Row
          key={coin.symbol}
          label={`${coinName(coin.symbol)}: ${coin.trades} صفقة، ربحت ${coin.wins}`}
          value={rText(coin.avg_r)}
          color={rColor(coin.avg_r)}
        />
      ))}
      {insights.notes.map((note) => (
        <Body key={note.symbol} color={colors.warn}>
          {coinName(note.symbol)} خاسرة في المتوسط بعد {note.trades} صفقة. هذا ملاحظة فقط؛ لا
          يغير البوت عملاته بنفسه، لأن الاختبار أثبت أن التبديل السريع يخسر. قرر أنت من تبويب
          الأمان.
        </Body>
      ))}
    </Card>
  );
}

export default function TradesScreen() {
  const { call, connection } = useBot();
  const [trades, setTrades] = useState<Trade[]>([]);
  const [events, setEvents] = useState<BotEvent[]>([]);
  const [insights, setInsights] = useState<Insights | null>(null);

  useFocusEffect(
    useCallback(() => {
      if (!connection) return;
      const load = () => {
        call<Trade[]>('/trades?limit=50', 'GET').then(setTrades).catch(() => undefined);
        call<BotEvent[]>('/events?limit=30', 'GET').then(setEvents).catch(() => undefined);
        call<Insights>('/insights', 'GET').then(setInsights).catch(() => undefined);
      };
      load();
      const timer = setInterval(load, 10000);
      return () => clearInterval(timer);
    }, [call, connection])
  );

  const closed = trades.filter((trade) => trade.closed_ms !== null);
  const total = closed.reduce((sum, trade) => sum + (trade.pnl ?? 0), 0);

  return (
    <Screen title="الصفقات">
      <Card>
        <Row label="الصفقات المقفلة" value={String(closed.length)} />
        <Row
          label="الصافي بعد الرسوم"
          value={signed(total)}
          color={total >= 0 ? colors.gain : colors.loss}
        />
      </Card>
      <Learned insights={insights} />
      {trades.length === 0 ? (
        <Card>
          <Body>
            لا توجد صفقات بعد. البوت يفحص السوق عند إغلاق كل شمعة 4 ساعات، وقد تمر أيام بدون
            صفقة، وهذا طبيعي في تتبع الاتجاه.
          </Body>
        </Card>
      ) : null}
      {trades.map((trade) => (
        <Card key={trade.id}>
          <Row
            label={`${trade.side === 'BUY' ? 'شراء' : 'بيع'} ${coinName(trade.symbol)}`}
            value={
              trade.closed_ms === null ? 'مفتوحة' : `${signed(trade.pnl)}  ${rText(trade.r)}`
            }
            color={trade.closed_ms === null ? colors.warn : rColor(trade.pnl)}
          />
          <Row label="الدخول" value={String(trade.entry_price)} />
          <Row label="الوقف" value={String(trade.stop_price)} />
          <Row label="الوقت" value={when(trade.opened_ms)} />
        </Card>
      ))}
      <Body color={colors.muted}>السجل</Body>
      {events.map((event) => (
        <Row key={event.id} label={when(event.at_ms)} value={message(event.code)} />
      ))}
    </Screen>
  );
}
