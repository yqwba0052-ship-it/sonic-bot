import { useState } from 'react';

import { Body, Button, Card, colors, Field, Screen } from '@/components/ui';
import type { Limits } from '@/lib/api';
import { errorCode, useBot } from '@/lib/bot-context';
import { message } from '@/lib/text';

type Key = keyof Limits;

const FIELDS: { key: Key; label: string; hint: string }[] = [
  {
    key: 'capital_usdt',
    label: 'المبلغ المخصص للتداول (USDT)',
    hint: 'أقصى هامش يستخدمه البوت. وقد يرفض السوق الصفقات إذا كان المبلغ صغيرًا.',
  },
  {
    key: 'max_daily_loss_usdt',
    label: 'أقصى خسارة يومية (USDT)',
    hint: 'عند الوصول إليها يقفل البوت الصفقات ويتوقف.',
  },
  { key: 'leverage', label: 'الرافعة المالية', hint: 'من 1 إلى 5.' },
  { key: 'risk_per_trade_pct', label: 'المخاطرة في الصفقة (%)', hint: 'من 0.1 إلى 5.' },
  { key: 'stop_loss_pct', label: 'وقف الخسارة (%)', hint: 'من 0.2 إلى 10.' },
  { key: 'take_profit_pct', label: 'جني الربح (%)', hint: 'من 0.2 إلى 30.' },
];

function initial(limits: Limits) {
  return Object.fromEntries(FIELDS.map(({ key }) => [key, String(limits[key])])) as Record<
    Key,
    string
  >;
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
  const [notice, setNotice] = useState<{ text: string; color: string } | null>(null);

  const save = async () => {
    const parsed = Object.fromEntries(
      FIELDS.map(({ key }) => [key, Number(values[key].replace(',', '.'))])
    ) as Limits;
    if (Object.values(parsed).some((value) => !Number.isFinite(value))) {
      setNotice({ text: message('invalid_values'), color: colors.loss });
      return;
    }
    try {
      await call('/limits', 'PUT', parsed);
      setNotice({ text: 'حُفظت حدود الأمان.', color: colors.gain });
    } catch (failure) {
      setNotice({ text: message(errorCode(failure)), color: colors.loss });
    }
  };

  return (
    <Screen title="الأمان">
      {status?.running ? (
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
            editable={!status?.running}
            value={values[key]}
            onChangeText={(text) => setValues({ ...values, [key]: text })}
          />
        ))}
      </Card>
      {notice ? <Body color={notice.color}>{notice.text}</Body> : null}
      <Button
        label="احفظ الحدود"
        disabled={!online || status?.running}
        onPress={save}
      />
    </Screen>
  );
}
