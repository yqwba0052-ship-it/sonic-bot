import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Modal, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { Body, Button, Card, colors, Field, Row, Screen } from '@/components/ui';
import { errorCode, useBot } from '@/lib/bot-context';
import { message, when } from '@/lib/text';

type Result = {
  trades: number;
  win_rate: number | null;
  net_pnl: number | null;
  net_pct?: number | null;
  max_drawdown: number | null;
};
type Trader = {
  trader_id: string;
  name: string;
  url: string | null;
  kind: string;
  status: 'watching' | 'paused';
  trades: number;
  model: null | {
    id: number;
    created_ms: number;
    learned: boolean;
    test?: { learned: Result; sonic: Result; days: number } | null;
  };
  paper: Result & { open: number };
};
type Report = {
  notes: string[];
  warning: string;
  learn?: { status: string; need?: number; train_trades?: number; test_trades?: number };
  model?: Model | null;
  test?: {
    days: number;
    from_ms: number;
    to_ms: number;
    learned: Result;
    sonic: Result;
    trader: { trades: number; win_rate: number | null; pnl_reported: number | null };
  };
  stats: { trades: number; usable: number };
};
type Model = {
  sides: string[];
  trend: string;
  btc: Record<string, boolean>;
  rsi: [number, number];
  age?: [number, number];
  stop_atr: number;
  target_atr: number;
  max_hold_bars: number;
  symbols: string[];
};
type Detail = {
  trader: Trader;
  trades: number;
  latest: null | { id: number; created_ms: number; model: Model | null; report: Report };
  versions: { id: number; created_ms: number; learned: number }[];
  paper: Result & { open: number };
};
type Ranked = {
  trader_id: string;
  name: string;
  status: string;
  model_id: number;
  score?: number;
  why?: string;
  result: (Result & {
    yearly_pct: number | null;
    profit_factor: number | null;
    months_positive: number | null;
    worst_trade: number | null;
    days: number;
  }) | null;
};
type Best = {
  trader_id: string;
  name: string;
  model_id: number;
  score: number;
  reasons: string[];
  paper?: Result & { open: number; needed: number };
};
type Coin = {
  symbol: string;
  strategies: number;
  same_period: boolean;
  from_ms: number | null;
  to_ms: number | null;
  ranked: Ranked[];
  excluded: Ranked[];
  best: Best | null;
  approved_model: number | null;
  better_than_approved: boolean;
};
type Ranking = { made_ms: number | null; total: number; coins: Record<string, Coin> };
type Account = { linked: boolean; key_hint: string | null; synced_ms: number | null; target: string | null };

const STUDY_MS = 180_000;

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(0)}%`;
}

function dollars(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}$`;
}

function rules(model: Model): string {
  const side = model.sides.map((s) => (s === 'BUY' ? 'شراء' : 'بيع')).join(' و');
  const trend = { with: 'مع الاتجاه', against: 'عكس الاتجاه', any: 'مع الاتجاه أو عكسه' }[model.trend];
  const btc = Object.values(model.btc).some(Boolean) ? '، وفقط عندما يوافق اتجاه BTC' : '';
  const age = !model.age
    ? ''
    : model.age[1] === 0
      ? '، عند أول شمعة بعد تقاطع المتوسطين'
      : `، بعد ${model.age[0]}–${model.age[1]} شمعة من بداية الاتجاه`;
  return (
    `${side} ${trend}${btc}، و RSI بين ${model.rsi[0]} و ${model.rsi[1]}${age}. ` +
    `الوقف ${model.stop_atr} ATR، والهدف ${model.target_atr} ATR، وأقصى مدة ${(model.max_hold_bars * 4) / 24} يوم. ` +
    `العملات: ${model.symbols.map((s) => s.replace('USDT', '')).join('، ')}.`
  );
}

function Strategies({ ranking }: { ranking: Ranking | null }) {
  if (!ranking) return null;
  const coins = Object.values(ranking.coins);
  return (
    <Card>
      <Body>أفضل استراتيجية لكل عملة ({ranking.total} استراتيجية محفوظة)</Body>
      <Body color={colors.muted}>
        إذا تداول متداولان أو أكثر نفس العملة تُقارن استراتيجياتهم على نفس الفترة (فترة لم تتعلم منها أي واحدة). وإذا
        كانت استراتيجية واحدة فقط تُقيَّم وحدها. المعايير: الربح بعد الرسوم واستقراره شهريًا، ومعامل الربح، وأقصى
        تراجع، وأسوأ صفقة، ثم نسبة الفوز. تدخل استراتيجيات المتداولين المحذوفين أيضًا، وتُعاد المقارنة بعد كل تحليل
        وكل يوم.
        {ranking.made_ms ? ` آخر مقارنة: ${when(ranking.made_ms)}.` : ''}
      </Body>
      {coins.length === 0 ? <Body color={colors.muted}>لا توجد استراتيجيات محللة بعد.</Body> : null}
      {coins.map((coin) => (
        <View key={coin.symbol} style={styles.rankRow}>
          <Text style={styles.rankName}>
            {coin.symbol.replace('USDT', '')}: {coin.strategies === 1 ? 'استراتيجية واحدة' : `${coin.strategies} استراتيجيات`}
            {coin.strategies > 1 ? (coin.same_period ? ' — على نفس الفترة' : ' — فترات مختلفة (التداخل قصير)') : ''}
          </Text>
          {coin.best ? (
            <View style={styles.champion}>
              <Text style={styles.championTitle}>
                الأفضل: {coin.best.name} (تقييم {coin.best.score})
              </Text>
              {coin.best.reasons.map((reason) => (
                <Text key={reason} style={styles.rankLine}>
                  • {reason}
                </Text>
              ))}
              {coin.best.paper ? (
                <Text style={styles.rankLine}>
                  تداولها الوهمي على هذه العملة: {coin.best.paper.trades} من {coin.best.paper.needed} صفقة، فوز{' '}
                  {percent(coin.best.paper.win_rate)}، {dollars(coin.best.paper.net_pnl)}
                </Text>
              ) : null}
            </View>
          ) : (
            <Text style={styles.rankLine}>لا توجد استراتيجية تجاوزت الشروط على هذه العملة.</Text>
          )}
          {coin.better_than_approved ? (
            <Text style={[styles.rankLine, { color: colors.warn }]}>
              يوجد أفضل من الاستراتيجية التي اعتمدتها لهذه العملة. لا تُستبدل ولا يتغير مبلغها إلا بموافقتك.
            </Text>
          ) : null}
          {coin.ranked.slice(1).map((item) => (
            <Text key={item.model_id} style={styles.rankLine}>
              {item.name}
              {item.status === 'removed' ? ' (محذوف)' : ''}: تقييم {item.score}، صافي {dollars(item.result?.net_pnl)}، معامل
              الربح {item.result?.profit_factor ?? '—'}
            </Text>
          ))}
          {coin.excluded.map((item) => (
            <Text key={item.model_id} style={styles.rankLine}>
              ✗ {item.name}
              {item.status === 'removed' ? ' (محذوف)' : ''}: {item.why}
            </Text>
          ))}
        </View>
      ))}
    </Card>
  );
}

function Compare({ report }: { report: Report }) {
  const test = report.test;
  if (!test) return null;
  const rows: [string, Result | null, string][] = [
    ['الاستراتيجية المستنتجة', test.learned, colors.accent],
    ['Sonic (الحالية)', test.sonic, colors.gain],
  ];
  return (
    <Card>
      <Body>
        الاختبار على فترة لم يتعلم منها ({test.days} يوم، من {when(test.from_ms)}). كل استراتيجية تبدأ بـ 30$،
        وتخاطر بـ 1% في الصفقة، مع الرسوم والانزلاق:
      </Body>
      <View style={styles.tableHead}>
        <Text style={[styles.cell, styles.first]}> </Text>
        <Text style={styles.cell}>الصفقات</Text>
        <Text style={styles.cell}>الفوز</Text>
        <Text style={styles.cell}>الصافي</Text>
        <Text style={styles.cell}>أقصى تراجع</Text>
      </View>
      {rows.map(([label, r, color]) => (
        <View key={label} style={styles.tableRow}>
          <Text style={[styles.cell, styles.first, { color }]}>{label}</Text>
          <Text style={styles.cell}>{r?.trades ?? '—'}</Text>
          <Text style={styles.cell}>{percent(r?.win_rate)}</Text>
          <Text style={[styles.cell, { color: (r?.net_pnl ?? 0) >= 0 ? colors.gain : colors.loss }]}>
            {dollars(r?.net_pnl)}
          </Text>
          <Text style={styles.cell}>{percent(r?.max_drawdown)}</Text>
        </View>
      ))}
      <View style={styles.tableRow}>
        <Text style={[styles.cell, styles.first, { color: colors.warn }]}>المتداول نفسه</Text>
        <Text style={styles.cell}>{test.trader.trades}</Text>
        <Text style={styles.cell}>{percent(test.trader.win_rate)}</Text>
        <Text style={styles.cell}>{dollars(test.trader.pnl_reported)}</Text>
        <Text style={styles.cell}>—</Text>
      </View>
      <Body color={colors.muted}>
        ربح المتداول كما في سجله (بمبالغه هو، فلا يقارَن بالدولار). أقصى تراجعه غير معروف من السجل.
      </Body>
    </Card>
  );
}

export default function TradersScreen() {
  const { call, connection } = useBot();
  const [list, setList] = useState<Trader[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');
  const [ref, setRef] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [csv, setCsv] = useState('');
  const [notice, setNotice] = useState<{ text: string; color: string } | null>(null);
  const [confirm, setConfirm] = useState<Trader | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [ranking, setRanking] = useState<Ranking | null>(null);
  const [key, setKey] = useState('');
  const [secret, setSecret] = useState('');

  const fail = (failure: unknown) => setNotice({ text: message(errorCode(failure)), color: colors.loss });

  const load = useCallback(async () => {
    if (!connection) return;
    try {
      setList(await call<Trader[]>('/traders', 'GET'));
      setAccount(await call<Account>('/account', 'GET'));
      setRanking(await call<Ranking>('/strategies', 'GET'));
    } catch {
      // the status line on the first tab already says when the server is unreachable
    }
  }, [call, connection]);

  const loadDetail = useCallback(
    async (id: string) => {
      try {
        setDetail(await call<Detail>(`/traders/${id}`, 'GET'));
      } catch (failure) {
        fail(failure);
      }
    },
    [call]
  );

  useFocusEffect(
    useCallback(() => {
      load();
    }, [load])
  );

  const add = async () => {
    try {
      const added = await call<Trader>('/traders', 'POST', { name: name.trim(), ref: ref.trim() });
      setAdding(false);
      setName('');
      setRef('');
      setNotice({ text: `أُضيف ${added.name}. استورد سجل صفقاته ليبدأ التعلم.`, color: colors.gain });
      await load();
      setOpen(added.trader_id);
      await loadDetail(added.trader_id);
    } catch (failure) {
      fail(failure);
    }
  };

  const toggle = async (trader: Trader) => {
    try {
      await call(`/traders/${trader.trader_id}`, 'PUT', {
        status: trader.status === 'watching' ? 'paused' : 'watching',
      });
      await load();
    } catch (failure) {
      fail(failure);
    }
  };

  const remove = async (trader: Trader) => {
    setConfirm(null);
    try {
      await call(`/traders/${trader.trader_id}?confirm=true`, 'DELETE');
      setNotice({
        text: `حُذف ${trader.name} من القائمة وتوقفت مراقبته. صفقاته واستراتيجياته محفوظة وتدخل في التقييم.`,
        color: colors.muted,
      });
      if (open === trader.trader_id) {
        setOpen(null);
        setDetail(null);
      }
      await load();
    } catch (failure) {
      fail(failure);
    }
  };

  const study = async (id: string) => {
    try {
      setNotice({ text: 'يحلل الصفقات ويختبر... قد يأخذ دقيقة.', color: colors.muted });
      await call(`/traders/${id}/study`, 'POST', {}, STUDY_MS);
      setNotice({ text: 'اكتمل التحليل وحُفظت نسخة جديدة من الاستراتيجية.', color: colors.gain });
      await loadDetail(id);
      await load();
    } catch (failure) {
      fail(failure);
    }
  };

  const importCsv = async (id: string) => {
    try {
      const result = await call<{ read: number; added: number; still_open: number; errors: [number, string][] }>(
        `/traders/${id}/import`,
        'POST',
        { csv },
        60_000
      );
      const bad = result.errors.length ? `، وأسطر فيها خطأ: ${result.errors.map((e) => e[0]).join('، ')}` : '';
      setNotice({
        text: `قُرئت ${result.read} صفقة، الجديد منها ${result.added}، والمفتوحة ${result.still_open}${bad}.`,
        color: result.added ? colors.gain : colors.warn,
      });
      setCsv('');
      if (result.added) await study(id);
    } catch (failure) {
      fail(failure);
    }
  };

  const link = async () => {
    try {
      setAccount(await call<Account>('/account', 'PUT', { key: key.trim(), secret: secret.trim() }, 30_000));
      setKey('');
      setSecret('');
      setNotice({ text: 'رُبط حسابك بمفتاح قراءة فقط (تحققت Binance من صلاحياته).', color: colors.gain });
    } catch (failure) {
      fail(failure);
    }
  };

  const sync = async () => {
    try {
      const result = await call<{ added: number; fills: number; trader_id: string }>(
        '/account/sync',
        'POST',
        { trader_id: open },
        STUDY_MS
      );
      setNotice({ text: `قُرئت ${result.fills} عملية، وأُضيفت ${result.added} صفقة إلى ${result.trader_id}.`, color: colors.gain });
      await load();
    } catch (failure) {
      fail(failure);
    }
  };

  const latest = detail?.latest;
  return (
    <Screen title="التعلم من المتداولين">
      <Card>
        <Body color={colors.muted}>
          تضيف متداولين بنفسك، ويتعلم Sonic من سجل صفقاتهم استراتيجية مستقلة لكل واحد، ويختبرها على فترة لم يتعلم منها
          ثم بالتداول الوهمي فقط. Binance لا توفر رسميًا صفقات متداول آخر، فتُستورد سجلاتهم يدويًا (CSV). لا يُستخدم أي
          شيء من هنا في تداول حقيقي.
        </Body>
        <Pressable accessibilityRole="button" onPress={() => setAdding((a) => !a)} style={styles.plus}>
          <Text style={styles.plusText}>{adding ? '× إغلاق' : '+ إضافة متداول'}</Text>
        </Pressable>
        {adding ? (
          <View>
            <Field label="الاسم" value={name} onChangeText={setName} placeholder="مثال: أبو فهد" />
            <Field
              label="رابط حسابه في Binance أو معرّفه"
              value={ref}
              onChangeText={setRef}
              autoCapitalize="none"
              placeholder="https://www.binance.com/.../lead-details/..."
              hint="يكفي الاسم إن لم يكن عندك الرابط. الرابط يُقرأ نصًا فقط ولا يُفتح."
            />
            <Button label="أضف" disabled={!name.trim() && !ref.trim()} onPress={add} />
          </View>
        ) : null}
      </Card>

      {notice ? <Body color={notice.color}>{notice.text}</Body> : null}

      <Strategies ranking={ranking} />

      {list.length === 0 ? (
        <Card>
          <Body color={colors.muted}>لم تضف أي متداول بعد. اضغط «+ إضافة متداول».</Body>
        </Card>
      ) : null}

      {list.map((trader) => (
        <Card key={trader.trader_id} tone={trader.status === 'watching' ? colors.gain : colors.muted}>
          <Pressable
            accessibilityRole="button"
            onPress={() => {
              const next = open === trader.trader_id ? null : trader.trader_id;
              setOpen(next);
              setDetail(null);
              if (next) loadDetail(next);
            }}>
            <Row
              label={`${trader.name}${trader.kind === 'account' ? ' (حسابي)' : ''}`}
              value={trader.status === 'watching' ? '● يُراقَب' : '⏸ موقوف'}
              color={trader.status === 'watching' ? colors.gain : colors.muted}
            />
            <Row label="الصفقات المحفوظة" value={String(trader.trades)} />
            {trader.model?.test ? (
              <Row
                label="المستنتجة في الاختبار"
                value={`فوز ${percent(trader.model.test.learned.win_rate)}، ${dollars(trader.model.test.learned.net_pnl)}`}
              />
            ) : null}
            {trader.paper.trades || trader.paper.open ? (
              <Row
                label="التداول الوهمي"
                value={`${trader.paper.trades} صفقة، فوز ${percent(trader.paper.win_rate)}، مفتوحة ${trader.paper.open}`}
              />
            ) : null}
          </Pressable>
          <View style={styles.actions}>
            <Pressable accessibilityRole="button" onPress={() => toggle(trader)} style={styles.small}>
              <Text style={styles.smallText}>{trader.status === 'watching' ? 'إيقاف المراقبة' : 'استئناف'}</Text>
            </Pressable>
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={`حذف ${trader.name}`}
              onPress={() => setConfirm(trader)}
              style={[styles.small, styles.danger]}>
              <Text style={styles.smallText}>حذف</Text>
            </Pressable>
          </View>

          {open === trader.trader_id && detail?.trader.trader_id === trader.trader_id ? (
            <View style={styles.detail}>
              <Body>استيراد سجل صفقاته (CSV): الصق النص هنا.</Body>
              <TextInput
                multiline
                value={csv}
                onChangeText={setCsv}
                placeholder={'symbol,side,leverage,qty,entry_price,exit_price,opened_at,closed_at,pnl,fee'}
                placeholderTextColor={colors.muted}
                style={styles.csv}
                accessibilityLabel="نص CSV"
              />
              <Body color={colors.muted}>
                سطر لكل صفقة مغلقة، والأوقات بتوقيت UTC. الخانات غير المعروفة اتركها فارغة، ولا تُخمَّن.
              </Body>
              <Button label="استورد وحلل" disabled={!csv.trim()} onPress={() => importCsv(trader.trader_id)} />
              <Button
                label="حدّث التحليل والاستراتيجية"
                color={colors.line}
                disabled={!detail.trades}
                onPress={() => study(trader.trader_id)}
              />
              {latest ? (
                <>
                  <Body>
                    آخر تحليل: {when(latest.created_ms)} (نسخة رقم {latest.id}، والنسخ المحفوظة {detail.versions.length}).
                  </Body>
                  {latest.report.notes.map((note) => (
                    <Body key={note}>• {note}</Body>
                  ))}
                  {latest.model ? (
                    <Body color={colors.accent}>القواعد المستنتجة: {rules(latest.model)}</Body>
                  ) : latest.report.learn?.status === 'not_enough_trades' ? (
                    <Body color={colors.warn}>
                      يحتاج {latest.report.learn.need} صفقة مغلقة على الأقل ليتعلم ويختبر. عنده الآن {latest.report.stats.usable}.
                    </Body>
                  ) : null}
                  <Compare report={latest.report} />
                  <Row
                    label="التداول الوهمي المستقل"
                    value={`${detail.paper.trades} صفقة، فوز ${percent(detail.paper.win_rate)}، ${dollars(detail.paper.net_pnl)}، تراجع ${percent(detail.paper.max_drawdown)}`}
                  />
                  <Body color={colors.warn}>{latest.report.warning} لن تُستخدم في تداول حقيقي إلا بموافقتك.</Body>
                </>
              ) : null}
            </View>
          ) : null}
        </Card>
      ))}

      <Card>
        <Body>ربط حسابك في Binance (قراءة فقط، اختياري)</Body>
        <Body color={colors.muted}>
          يقرأ سجل صفقاتك في العقود الآجلة لآخر 3 أشهر تقريبًا. يُرفض أي مفتاح فيه صلاحية تداول أو تحويل أو سحب. Binance
          لا تذكر من أي متداول جاءت صفقة النسخ، فتذهب إلى «صفقات حسابي» إلا إذا فتحت متداولًا قبل المزامنة.
        </Body>
        {account?.linked ? (
          <>
            <Row label="المفتاح" value={account.key_hint ?? ''} />
            <Row label="آخر مزامنة" value={account.synced_ms ? when(account.synced_ms) : 'لم تتم'} />
            <Button label={open ? `زامن إلى المتداول المفتوح` : 'زامن إلى «صفقات حسابي»'} onPress={sync} />
            <Button
              label="فك الربط"
              color={colors.line}
              onPress={async () => {
                setAccount(await call<Account>('/account', 'DELETE'));
              }}
            />
          </>
        ) : (
          <>
            <Field label="API Key" value={key} onChangeText={setKey} autoCapitalize="none" />
            <Field label="Secret Key" value={secret} onChangeText={setSecret} autoCapitalize="none" secureTextEntry />
            <Button label="تحقق واربط" disabled={!key.trim() || !secret.trim()} onPress={link} />
          </>
        )}
      </Card>

      <Modal transparent visible={confirm !== null} animationType="fade" onRequestClose={() => setConfirm(null)}>
        <View style={styles.backdrop}>
          <View style={styles.dialog}>
            <Text style={styles.dialogTitle}>هل أنت متأكد؟</Text>
            <Text style={styles.dialogText}>
              سيُحذف {confirm?.name} من القائمة وتتوقف مراقبته. تبقى صفقاته واستراتيجياته ونتائج اختباراتها
              محفوظة، وتدخل في تقييم الاستراتيجيات.
            </Text>
            <View style={styles.actions}>
              <Pressable
                accessibilityRole="button"
                onPress={() => confirm && remove(confirm)}
                style={[styles.small, styles.danger]}>
                <Text style={styles.smallText}>موافق</Text>
              </Pressable>
              <Pressable accessibilityRole="button" onPress={() => setConfirm(null)} style={styles.small}>
                <Text style={styles.smallText}>إلغاء</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </Screen>
  );
}

const styles = StyleSheet.create({
  plus: {
    alignSelf: 'flex-start',
    backgroundColor: colors.accent,
    borderRadius: 10,
    paddingHorizontal: 16,
    paddingVertical: 10,
    marginTop: 8,
  },
  plusText: { color: '#fff', fontSize: 16, fontWeight: '700' },
  actions: { flexDirection: 'row', gap: 8, marginTop: 8 },
  small: { backgroundColor: colors.line, borderRadius: 8, paddingHorizontal: 14, paddingVertical: 9 },
  smallText: { color: colors.text, fontSize: 14, fontWeight: '700' },
  danger: { backgroundColor: colors.loss },
  detail: { marginTop: 10, gap: 6 },
  csv: {
    minHeight: 110,
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 8,
    color: colors.text,
    padding: 8,
    fontSize: 12,
    textAlignVertical: 'top',
    writingDirection: 'ltr',
    textAlign: 'left',
  },
  tableHead: { flexDirection: 'row', borderBottomWidth: 1, borderColor: colors.line, paddingVertical: 4 },
  tableRow: { flexDirection: 'row', paddingVertical: 4 },
  cell: { flex: 1, color: colors.text, fontSize: 12, textAlign: 'center' },
  first: { flex: 1.8, textAlign: 'right', fontWeight: '700' },
  champion: { borderWidth: 1, borderColor: colors.gain, borderRadius: 10, padding: 10, gap: 4 },
  championTitle: { color: colors.gain, fontSize: 15, fontWeight: '700' },
  rankRow: { borderTopWidth: 1, borderColor: colors.line, paddingVertical: 6 },
  rankName: { color: colors.text, fontSize: 14, fontWeight: '700' },
  rankLine: { color: colors.muted, fontSize: 12, lineHeight: 18 },
  backdrop: { flex: 1, backgroundColor: '#000a', justifyContent: 'center', padding: 24 },
  dialog: { backgroundColor: colors.card, borderRadius: 12, padding: 18, gap: 10 },
  dialogTitle: { color: colors.text, fontSize: 18, fontWeight: '700' },
  dialogText: { color: colors.muted, fontSize: 14, lineHeight: 22 },
});
