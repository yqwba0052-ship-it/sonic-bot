import { useState } from 'react';

import { Body, Button, Card, colors, ConfirmButton, Row, Screen } from '@/components/ui';
import { errorCode, useBot } from '@/lib/bot-context';
import { message, money, signed } from '@/lib/text';

const MODE_LABEL = { paper: 'رصيد وهمي بأسعار حقيقية', demo: 'حساب Binance التجريبي' };

function pnlColor(value: number) {
  if (value > 0) return colors.gain;
  return value < 0 ? colors.loss : undefined;
}

export default function StatusScreen() {
  const { connection, status, online, error, call } = useBot();
  const [actionError, setActionError] = useState('');

  const act = async (path: string) => {
    setActionError('');
    try {
      await call(path);
    } catch (failure) {
      setActionError(message(errorCode(failure)));
    }
  };

  if (!connection) {
    return (
      <Screen title="الحالة">
        <Card>
          <Body>افتح تبويب «الاتصال» وأدخل عنوان الخادم ورمز الدخول.</Body>
        </Card>
      </Screen>
    );
  }

  const today = (status?.realized_today ?? 0) + (status?.unrealized ?? 0);
  const state = status?.halted ? 'موقوف للأمان' : status?.running ? 'يعمل' : 'متوقف';
  const stateColor = status?.halted ? colors.loss : status?.running ? colors.gain : colors.warn;

  return (
    <Screen title="الحالة">
      {!online ? (
        <Card tone={colors.loss}>
          <Body color={colors.loss}>{message(error || 'offline')}</Body>
        </Card>
      ) : null}
      {status ? (
        <>
          <Card tone={colors.warn}>
            <Body color={colors.warn}>
              {MODE_LABEL[status.mode]}. الاستراتيجية تجريبية وغير مثبتة الربح.
            </Body>
          </Card>
          <Card>
            <Row label="حالة البوت" value={state} color={stateColor} />
            <Row label="الرصيد" value={money(status.balance)} />
            <Row label="ربح أو خسارة اليوم" value={signed(today)} color={pnlColor(today)} />
            <Row label="المبلغ المخصص" value={money(status.limits.capital_usdt)} />
            <Row label="حد الخسارة اليومي" value={money(status.limits.max_daily_loss_usdt)} />
          </Card>
          {status.halted ? (
            <Card tone={colors.loss}>
              <Body>{message(status.halted)}</Body>
              <ConfirmButton
                label="إعادة التفعيل"
                confirmLabel="اضغط مرة ثانية للتأكيد"
                color={colors.warn}
                onPress={() => act('/clear-halt')}
              />
            </Card>
          ) : null}
          {status.last_error ? (
            <Card tone={colors.warn}>
              <Body color={colors.muted}>{status.last_error}</Body>
            </Card>
          ) : null}
          <Card>
            <Body>الصفقات المفتوحة: {status.open_trades.length}</Body>
            {status.open_trades.map((trade) => {
              const live = status.positions[trade.symbol];
              return (
                <Row
                  key={trade.id}
                  label={`${trade.side === 'BUY' ? 'شراء' : 'بيع'} ${trade.symbol}`}
                  value={signed(live?.unrealized)}
                  color={pnlColor(live?.unrealized ?? 0)}
                />
              );
            })}
          </Card>
          {actionError ? <Body color={colors.loss}>{actionError}</Body> : null}
          {status.running ? (
            <Button label="أوقف الصفقات الجديدة" color={colors.warn} onPress={() => act('/stop')} />
          ) : (
            <Button
              label="شغّل البوت"
              color={colors.gain}
              disabled={!online || Boolean(status.halted)}
              onPress={() => act('/start')}
            />
          )}
          <ConfirmButton
            label="أقفل كل الصفقات وأوقف البوت"
            confirmLabel="اضغط مرة ثانية للإغلاق الآن"
            color={colors.loss}
            disabled={!online}
            onPress={() => act('/panic')}
          />
        </>
      ) : null}
    </Screen>
  );
}
