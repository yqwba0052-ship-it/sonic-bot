import { useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { Body, Button, Card, colors, Field, Row } from '@/components/ui';
import { when } from '@/lib/text';

export type KopyonStatus = {
  linked: boolean;
  source: 'env' | 'app' | null;
  hint: string | null;
  every_minutes: number;
  used_30d: number;
  budget_30d: number;
  free_plan_30d: number;
  state: Record<
    string,
    { last_ms?: number; collected?: number; last_added?: number; last_error?: string | null; backfilled?: boolean }
  >;
};
type Call = <T>(path: string, method?: string, body?: unknown, timeoutMs?: number) => Promise<T>;

export const KOPYON_ERRORS: Record<string, string> = {
  kopyon_key_invalid: 'مفتاح Kopyon غير صحيح أو أُلغي',
  kopyon_forbidden: 'Kopyon رفض الطلب (قيود المفتاح)',
  kopyon_unknown_trader: 'Kopyon لا يعرف هذا المتداول أو محفظته مغلقة',
  kopyon_limit: 'تجاوز حد Kopyon مؤقتًا',
  kopyon_budget: 'توقف لحماية حد الخطة المجانية الشهري',
  kopyon_unreachable: 'تعذر الوصول إلى Kopyon',
};

export function kopyonError(code: string | null | undefined): string {
  if (!code) return '';
  return KOPYON_ERRORS[code] ?? code;
}

export function KopyonCard({
  data,
  names,
  call,
  report,
}: {
  data: KopyonStatus | null;
  names: Record<string, string>;
  call: Call;
  report: (text: string, ok: boolean) => Promise<void>;
}) {
  const [key, setKey] = useState('');
  if (!data) return null;
  return (
    <Card>
      <Body>Kopyon: جمع صفقات المتداولين تلقائيًا</Body>
      <Body color={colors.muted}>
        مصدر خارجي غير رسمي، لا تتبع لـ Binance: يبني بياناته مما تعرضه Binance علنًا عن المتداولين، ولا يضمن دقتها. يجلب
        الصفقات المغلقة فقط (الأوقات، والأسعار، والكمية، والرافعة، والربح الإجمالي). Binance لا تنشر الرسوم، فتبقى فارغة.
        مفتاحه منفصل عن مفتاح Binance، ولا يُرسل مفتاح Binance إلى Kopyon أبدًا.
      </Body>
      {data.linked ? (
        <>
          <Row label="المفتاح (لا يُعرض بعد الحفظ)" value={data.hint ?? ''} />
          <Row label="مصدره" value={data.source === 'env' ? 'ملف الإعدادات على الخادم' : 'التطبيق'} />
          <Row label="الجمع التلقائي" value={`كل ${data.every_minutes} دقائق`} color={colors.gain} />
          <Row
            label="طلبات آخر 30 يومًا"
            value={`${data.used_30d} من ${data.budget_30d} (الخطة المجانية ${data.free_plan_30d})`}
            color={data.used_30d >= data.budget_30d ? colors.loss : undefined}
          />
          {Object.entries(data.state).map(([id, st]) => (
            <View key={id} style={styles.trader}>
              <Text style={styles.name}>{names[id] ?? id}</Text>
              <Text style={styles.line}>
                آخر مزامنة: {st.last_ms ? when(st.last_ms) : '—'}، جُمعت {st.collected ?? 0} صفقة
                {st.last_added !== undefined ? `، آخر مرة +${st.last_added}` : ''}
              </Text>
              {st.last_error ? (
                <Text style={[styles.line, { color: colors.loss }]}>خطأ: {kopyonError(st.last_error)}</Text>
              ) : null}
            </View>
          ))}
          <Button
            label="زامن الآن"
            color={colors.line}
            onPress={async () => {
              try {
                await call('/kopyon/sync', 'POST', {}, 180_000);
                await report('اكتملت مزامنة Kopyon.', true);
              } catch (failure) {
                await report(failure instanceof Error ? failure.message : 'invalid_values', false);
              }
            }}
          />
          {data.source !== 'env' ? (
            <Button
              label="احذف مفتاح Kopyon"
              color={colors.line}
              onPress={async () => {
                await call('/kopyon', 'DELETE');
                await report('حُذف مفتاح Kopyon. الصفقات المجموعة باقية.', true);
              }}
            />
          ) : null}
        </>
      ) : (
        <>
          <Field
            label="مفتاح Kopyon API"
            value={key}
            onChangeText={setKey}
            autoCapitalize="none"
            secureTextEntry
            hint="يُحفظ على الخادم فقط، ولا يظهر بعد الحفظ."
          />
          <Button
            label="تحقق واحفظ"
            disabled={!key.trim()}
            onPress={async () => {
              try {
                await call('/kopyon', 'PUT', { key: key.trim() }, 60_000);
                setKey('');
                await report('حُفظ مفتاح Kopyon. يبدأ جلب السجل الآن، ثم كل 10 دقائق.', true);
              } catch (failure) {
                await report(failure instanceof Error ? failure.message : 'invalid_values', false);
              }
            }}
          />
        </>
      )}
    </Card>
  );
}

const styles = StyleSheet.create({
  trader: { borderTopWidth: 1, borderColor: colors.line, paddingTop: 6, marginTop: 4 },
  name: { color: colors.text, fontSize: 14, fontWeight: '700' },
  line: { color: colors.muted, fontSize: 12, lineHeight: 18 },
});
