import { useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { Body, Button, colors, Field } from '@/components/ui';

export type Draft = {
  id: number;
  symbol: string;
  side: 'BUY' | 'SELL';
  leverage: number | null;
  qty: number | null;
  entry_price: number;
  exit_price: number;
  pnl: number | null;
  missing: string[];
};
export type ImportRow = { line: number; status: 'saved' | 'draft' | 'open' | 'error'; missing: string[] };

export const FIELD_NAMES: Record<string, string> = {
  opened_at: 'وقت الدخول',
  closed_at: 'وقت الخروج',
  fee: 'الرسوم',
  leverage: 'الرافعة',
  qty: 'الكمية',
  pnl: 'الربح',
  exit_price: 'سعر الخروج',
  entry_price: 'سعر الدخول',
  symbol: 'العملة',
  side: 'الاتجاه',
};
const STATUS: Record<ImportRow['status'], string> = {
  saved: 'حُفظت',
  draft: 'مسودة',
  open: 'مفتوحة (تُركت)',
  error: 'خطأ',
};

export function names(fields: string[]): string {
  return fields.map((f) => FIELD_NAMES[f] ?? f).join('، ');
}

/** One line per imported row: what happened to it and which fields it lacks. */
export function ImportReport({ rows }: { rows: ImportRow[] }) {
  if (!rows.length) return null;
  return (
    <View style={styles.box}>
      <Body>نتيجة كل سطر:</Body>
      {rows.slice(0, 60).map((row) => (
        <Text
          key={row.line}
          style={[styles.line, { color: row.status === 'saved' ? colors.gain : row.status === 'error' ? colors.loss : colors.warn }]}>
          سطر {row.line}: {STATUS[row.status]}
          {row.missing.length ? ` — ناقص: ${names(row.missing)}` : ''}
        </Text>
      ))}
    </View>
  );
}

function OneDraft({ draft, onSave }: { draft: Draft; onSave: (id: number, opened: string, closed: string, fee: string) => Promise<void> }) {
  const [opened, setOpened] = useState('');
  const [closed, setClosed] = useState('');
  const [fee, setFee] = useState('');
  return (
    <View style={styles.draft}>
      <Text style={styles.title}>
        {draft.symbol.replace('USDT', '')} {draft.side === 'BUY' ? 'شراء' : 'بيع'}
        {draft.leverage ? ` ×${draft.leverage}` : ''}: {draft.entry_price} ← {draft.exit_price}
        {draft.pnl !== null ? `، الربح ${draft.pnl}` : ''}
      </Text>
      <Text style={[styles.line, { color: colors.warn }]}>ناقص: {names(draft.missing)}</Text>
      <Field label="وقت الدخول (UTC)" value={opened} onChangeText={setOpened} placeholder="2026-11-10 14:30" autoCapitalize="none" />
      <Field label="وقت الخروج (UTC)" value={closed} onChangeText={setClosed} placeholder="2026-11-10 18:05" autoCapitalize="none" />
      <Field label="الرسوم (اختياري، اتركها فارغة إن لم تعرفها)" value={fee} onChangeText={setFee} keyboardType="decimal-pad" />
      <Button
        label="أكمل الصفقة"
        disabled={!opened.trim() || !closed.trim()}
        onPress={() => onSave(draft.id, opened.trim(), closed.trim(), fee.trim())}
      />
    </View>
  );
}

/** Drafts: prices known, times missing. Kept as they are; never used for learning or paper
 * trading until the real times are added. */
export function Drafts({ drafts, onSave }: { drafts: Draft[]; onSave: (id: number, opened: string, closed: string, fee: string) => Promise<void> }) {
  if (!drafts.length) return null;
  return (
    <View style={styles.box}>
      <Body color={colors.warn}>
        مسودات ({drafts.length}): صفقات بلا وقت دخول أو خروج. محفوظة كما هي، ولا تدخل في التعلم ولا في التداول الوهمي حتى
        تُضاف أوقاتها الحقيقية هنا، أو باستيراد السجل نفسه مع الأوقات.
      </Body>
      {drafts.map((draft) => (
        <OneDraft key={draft.id} draft={draft} onSave={onSave} />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  box: { borderWidth: 1, borderColor: colors.line, borderRadius: 10, padding: 10, gap: 6, marginTop: 6 },
  draft: { borderTopWidth: 1, borderColor: colors.line, paddingTop: 8, gap: 4 },
  title: { color: colors.text, fontSize: 14, fontWeight: '700' },
  line: { fontSize: 12, lineHeight: 18 },
});
