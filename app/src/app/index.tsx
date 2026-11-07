import { useState } from 'react';

import { Body, Button, Card, colors, ConfirmButton, Row, Screen } from '@/components/ui';
import { errorCode, useBot } from '@/lib/bot-context';
import { coinName, message, money, signed, when } from '@/lib/text';

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
              {MODE_LABEL[status.mode]}. الاستراتيجية: تتبع الاتجاه على شموع 4 ساعات. ربحت في اختبار 2021–2026، لكن النتائج الماضية لا تضمن المستقبل.
            </Body>
          </Card>
          <Card>
            <Row label="حالة البوت" value={state} color={stateColor} />
            <Row label="الرصيد" value={money(status.balance)} />
            <Row label="ربح أو خسارة اليوم" value={signed(today)} color={pnlColor(today)} />
            <Row label="المبلغ المخصص" value={money(status.limits.capital_usdt)} />
            <Row
              label="المتبقي من المبلغ"
              value={money(status.budget_left)}
              color={status.budget_left < status.limits.capital_usdt ? colors.warn : undefined}
            />
            <Row label="العملات" value={status.limits.symbols.map(coinName).join('، ')} />
            <Row label="حد الخسارة اليومي" value={money(status.limits.max_daily_loss_usdt)} />
            <Row label="الفحص القادم" value={when(status.next_check_ms)} />
          </Card>
          {status.limits.capital_usdt < 30 ? (
            <Card tone={colors.warn}>
              <Body color={colors.warn}>
                المبلغ أقل من 30$، وهو أقل مبلغ نجح في الاختبار. سيتخطى البوت صفقات كثيرة.
              </Body>
            </Card>
          ) : null}
          {status.limits.symbols
            .filter((symbol) => (status.coin_min_amount[symbol] ?? 0) > status.budget_left)
            .map((symbol) => (
              <Card key={symbol} tone={colors.warn}>
                <Body color={colors.warn}>
                  {coinName(symbol)} تحتاج اليوم {status.coin_min_amount[symbol]}$ على الأقل،
                  فلن يتداول البوت فيها بالمبلغ المتبقي.
                </Body>
              </Card>
            ))}
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
