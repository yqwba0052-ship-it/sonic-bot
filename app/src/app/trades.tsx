import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';

import { Body, Card, colors, Row, Screen } from '@/components/ui';
import type { BotEvent, Trade } from '@/lib/api';
import { useBot } from '@/lib/bot-context';
import { message, signed, when } from '@/lib/text';

export default function TradesScreen() {
  const { call, connection } = useBot();
  const [trades, setTrades] = useState<Trade[]>([]);
  const [events, setEvents] = useState<BotEvent[]>([]);

  useFocusEffect(
    useCallback(() => {
      if (!connection) return;
      const load = () => {
        call<Trade[]>('/trades?limit=50', 'GET').then(setTrades).catch(() => undefined);
        call<BotEvent[]>('/events?limit=30', 'GET').then(setEvents).catch(() => undefined);
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
      {trades.length === 0 ? (
        <Card>
          <Body>لا توجد صفقات بعد.</Body>
        </Card>
      ) : null}
      {trades.map((trade) => (
        <Card key={trade.id}>
          <Row
            label={`${trade.side === 'BUY' ? 'شراء' : 'بيع'} ${trade.symbol}`}
            value={trade.closed_ms === null ? 'مفتوحة' : signed(trade.pnl)}
            color={
              trade.closed_ms === null
                ? colors.warn
                : (trade.pnl ?? 0) >= 0
                  ? colors.gain
                  : colors.loss
            }
          />
          <Row label="الدخول" value={String(trade.entry_price)} />
          <Row label="الوقف" value={String(trade.stop_price)} />
          <Row label="الهدف" value={String(trade.target_price)} />
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
