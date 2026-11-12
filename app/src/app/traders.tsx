import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';
import { Modal, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { type Allocations, AllocationsCard } from '@/components/allocations';
import { type Draft, Drafts, ImportReport, type ImportRow } from '@/components/drafts';
import { KopyonCard, kopyonError, type KopyonStatus } from '@/components/kopyon';
import { Body, Button, Card, colors, Field, Row, Screen } from '@/components/ui';
import { errorCode, useBot } from '@/lib/bot-context';
import { deviceOffsetMinutes, offsetLabel, pickCsvText, pickScreenshotBase64 } from '@/lib/pick';
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
  drafts: number;
  collection: {
    automatic: boolean;
    source: string;
    every_hours?: number;
    last_ms: number | null;
    linked?: boolean;
    synced_ms?: number | null;
    kopyon?: {
      linked: boolean;
      has_binance_id: boolean;
      active: boolean;
      every_minutes: number;
      last_ms: number | null;
      collected: number;
      last_added: number | null;
      error: string | null;
    };
  };
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
  drafts: Draft[];
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
type Account = {
  linked: boolean;
  source: 'env' | 'app' | null;
  key_hint: string | null;
  synced_ms: number | null;
  target: string | null;
  ip_restricted: boolean | null;
  server_ip: string | null;
  every_hours: number;
};
type PreviewTrade = {
  symbol: string;
  side: string;
  leverage: number | null;
  qty: number | null;
  entry_price: number | null;
  exit_price: number | null;
  opened_ms: number | null;
  closed_ms: number | null;
  pnl: number | null;
  fee: number | null;
};
type Preview = {
  preview_id: string;
  trader: string;
  trades: PreviewTrade[];
  drafts: PreviewTrade[];
  rows: ImportRow[];
  ocr_text: string;
};
type AccountTest = {
  read_only: boolean;
  ip_restricted: boolean;
  balance: { wallet: number; available: number };
  income_30d: number;
  symbols_30d: string[];
  synced_trades: number;
};

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

function Approve({ modelId, approved, onApprove }: { modelId: number; approved: boolean; onApprove: (id: number) => void }) {
  return approved ? (
    <Text style={[styles.rankLine, { color: colors.gain }]}>✓ معتمدة</Text>
  ) : (
    <Pressable accessibilityRole="button" accessibilityLabel="اعتماد الاستراتيجية" onPress={() => onApprove(modelId)} style={styles.approve}>
      <Text style={styles.smallText}>✓ اعتماد</Text>
    </Pressable>
  );
}

function Strategies({
  ranking,
  approvedIds,
  onApprove,
}: {
  ranking: Ranking | null;
  approvedIds: Set<number>;
  onApprove: (id: number) => void;
}) {
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
              <Approve modelId={coin.best.model_id} approved={approvedIds.has(coin.best.model_id)} onApprove={onApprove} />
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
            <View key={item.model_id}>
              <Text style={styles.rankLine}>
                {item.name}
                {item.status === 'removed' ? ' (محذوف)' : ''}: تقييم {item.score}، صافي {dollars(item.result?.net_pnl)}، معامل
                الربح {item.result?.profit_factor ?? '—'}
              </Text>
              <Approve modelId={item.model_id} approved={approvedIds.has(item.model_id)} onApprove={onApprove} />
            </View>
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
  const [tested, setTested] = useState<AccountTest | null>(null);
  const [rows, setRows] = useState<ImportRow[]>([]);
  const [zone, setZone] = useState<'device' | 'utc'>('device');
  const [ocrText, setOcrText] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [kopyonStatus, setKopyonStatus] = useState<KopyonStatus | null>(null);
  const [allocations, setAllocations] = useState<Allocations | null>(null);
  const [key, setKey] = useState('');
  const [secret, setSecret] = useState('');

  const fail = (failure: unknown) => setNotice({ text: message(errorCode(failure)), color: colors.loss });

  const load = useCallback(async () => {
    if (!connection) return;
    try {
      setList(await call<Trader[]>('/traders', 'GET'));
      setAccount(await call<Account>('/account', 'GET'));
      setRanking(await call<Ranking>('/strategies', 'GET'));
      setAllocations(await call<Allocations>('/allocations', 'GET'));
      setKopyonStatus(await call<KopyonStatus>('/kopyon', 'GET'));
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

  type ImportResult = {
    read: number;
    added: number;
    drafts: number;
    drafts_completed: number;
    still_open: number;
    errors: [number, string][];
    rows: ImportRow[];
    complete_trades: number;
    study_started: boolean;
    ocr_text?: string;
  };

  const offset = zone === 'utc' ? 0 : deviceOffsetMinutes();

  const showImport = async (id: string, result: ImportResult) => {
    const bad = result.errors.length ? `، وأسطر فيها خطأ: ${result.errors.map((e) => e[0]).join('، ')}` : '';
    const drafts = result.drafts ? `، ومسودات ناقصة الأوقات ${result.drafts}` : '';
    const done = result.drafts_completed ? `، وأُكملت ${result.drafts_completed} مسودة تلقائيًا` : '';
    const study = result.study_started
      ? ' بدأ تحليل استراتيجيته تلقائيًا في الخلفية.'
      : result.complete_trades < 20
        ? ` يبدأ التحليل تلقائيًا عند 20 صفقة مكتملة (عنده ${result.complete_trades}).`
        : '';
    setNotice({
      text: `صفقات مكتملة جديدة ${result.added}${done}${drafts}، ومفتوحة تُركت ${result.still_open}${bad}.${study}`,
      color: result.added || result.drafts_completed ? colors.gain : colors.warn,
    });
    setRows(result.rows);
    setOcrText(result.ocr_text ?? '');
    await loadDetail(id);
    await load();
  };

  const importText = async (id: string, text: string) => {
    try {
      const result = await call<ImportResult>(
        `/traders/${id}/import`,
        'POST',
        { csv: text, tz_offset_min: offset },
        60_000
      );
      setCsv('');
      await showImport(id, result);
    } catch (failure) {
      fail(failure);
    }
  };

  const importCsv = (id: string) => importText(id, csv);

  const importFile = async (id: string) => {
    try {
      const picked = await pickCsvText();
      if (!picked) return;
      setNotice({ text: `يقرأ ${picked.name}...`, color: colors.muted });
      await importText(id, picked.text);
    } catch (failure) {
      fail(failure);
    }
  };

  const importImage = async (id: string) => {
    try {
      const image = await pickScreenshotBase64();
      if (!image) return;
      setNotice({ text: 'يقرأ الصورة على الخادم (OCR)... قد يأخذ نصف دقيقة.', color: colors.muted });
      const read = await call<Preview>(
        `/traders/${id}/import-image`,
        'POST',
        { image_base64: image, tz_offset_min: deviceOffsetMinutes() },
        150_000
      );
      setPreview({ ...read, trader: id });
      setOcrText(read.ocr_text);
      setNotice({
        text: 'راجع ما قُرئ من الصورة، ثم اضغط «اعتمد» ليُحفظ. لا يُحفظ شيء قبل موافقتك.',
        color: colors.warn,
      });
    } catch (failure) {
      fail(failure);
    }
  };

  const confirmPreview = async () => {
    if (!preview) return;
    try {
      const result = await call<ImportResult>(
        `/traders/${preview.trader}/import-preview/${preview.preview_id}`,
        'POST',
        {},
        60_000
      );
      setPreview(null);
      await showImport(preview.trader, result);
    } catch (failure) {
      fail(failure);
    }
  };

  const saveDraft = async (draftId: number, opened: string, closed: string, fee: string) => {
    if (!open) return;
    try {
      await call(`/traders/${open}/drafts/${draftId}`, 'PUT', { opened_at: opened, closed_at: closed, fee });
      setNotice({ text: 'اكتملت الصفقة وانتقلت من المسودات إلى الصفقات المحفوظة.', color: colors.gain });
      await loadDetail(open);
      await load();
    } catch (failure) {
      fail(failure);
    }
  };

  const testAccount = async () => {
    try {
      setTested(await call<AccountTest>('/account/test', 'POST', {}, 120_000));
      setNotice({ text: 'نجح الاتصال: قُرئ الرصيد وسجل العقود (قراءة فقط).', color: colors.gain });
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
      await testAccount();
    } catch (failure) {
      fail(failure);
    }
  };

  const sync = async () => {
    try {
      const result = await call<{ added: number; fills: number }>('/account/sync', 'POST', {}, STUDY_MS);
      setNotice({
        text: `قُرئت ${result.fills} عملية، وأُضيفت ${result.added} صفقة إلى «صفقات حسابي» (لا تختلط بالمتداولين).`,
        color: colors.gain,
      });
      await load();
    } catch (failure) {
      fail(failure);
    }
  };

  const report = async (text: string, ok: boolean) => {
    const shown = ok
      ? text
      : text.startsWith('gate_not_met')
        ? 'لم تكتمل شروط التفعيل. اضغط «افحص الشروط» لترى الناقص.'
        : message(text);
    setNotice({ text: shown, color: ok ? colors.gain : colors.loss });
    await load();
  };

  const approve = async (id: number) => {
    try {
      await call(`/allocations/${id}/approve`, 'POST');
      await report('اعتُمدت الاستراتيجية (بلا مال ولا تداول). حدّد مبلغها من «الاستراتيجيات المعتمدة».', true);
    } catch (failure) {
      fail(failure);
    }
  };

  const approvedIds = new Set((allocations?.items ?? []).filter((a) => a.state !== 'stopped').map((a) => a.model_id));
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

      <Strategies ranking={ranking} approvedIds={approvedIds} onApprove={approve} />
      <KopyonCard
        data={kopyonStatus}
        names={Object.fromEntries(list.map((t) => [t.trader_id, t.name]))}
        call={call}
        report={report}
      />
      <AllocationsCard data={allocations} call={call} report={report} />

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
              value={trader.status === 'watching' ? '● مفعّل للتحليل' : '⏸ موقوف'}
              color={trader.status === 'watching' ? colors.gain : colors.muted}
            />
            {trader.collection.automatic ? (
              <Row
                label="جمع الصفقات"
                value={
                  trader.collection.linked
                    ? `تلقائي كل ${trader.collection.every_hours} ساعات (حسابك، قراءة فقط)`
                    : 'متوقف: الحساب غير مربوط'
                }
                color={trader.collection.linked ? colors.gain : colors.warn}
              />
            ) : trader.collection.kopyon?.active ? (
              <Row
                label="جمع الصفقات"
                value={`تلقائي كل ${trader.collection.kopyon.every_minutes} دقائق عبر Kopyon (غير رسمي)`}
                color={colors.gain}
              />
            ) : (
              <Row
                label="جمع الصفقات"
                value={
                  trader.collection.kopyon && !trader.collection.kopyon.has_binance_id
                    ? 'يدوي: أضفه برابطه في Binance ليُجمع تلقائيًا'
                    : trader.status !== 'watching'
                      ? 'متوقف (المتداول موقوف)'
                      : 'يدوي: أضف مفتاح Kopyon للجمع التلقائي'
                }
                color={colors.warn}
              />
            )}
            {trader.collection.kopyon?.active ? (
              <Row
                label="Kopyon"
                value={`آخر مزامنة ${trader.collection.kopyon.last_ms ? when(trader.collection.kopyon.last_ms) : '—'}، جُمعت ${trader.collection.kopyon.collected}`}
              />
            ) : null}
            {trader.collection.kopyon?.error ? (
              <Row label="خطأ اتصال" value={kopyonError(trader.collection.kopyon.error)} color={colors.loss} />
            ) : null}
            {trader.collection.automatic && trader.collection.synced_ms ? (
              <Row label="آخر مزامنة" value={when(trader.collection.synced_ms)} />
            ) : null}
            <Row
              label="آخر صفقة وصلت"
              value={trader.collection.last_ms ? when(trader.collection.last_ms) : 'لم تصل أي صفقة بعد'}
            />
            {!trader.collection.automatic && !trader.collection.kopyon?.active ? (
              <Text style={styles.rankLine}>
                Binance لا توفّر رسميًا صفقات المتداولين الآخرين، ولا تظهر صفقات النسخ في حسابك عبر المفتاح. لذلك لا
                يجمع Sonic صفقاته بنفسه: يتعلم فقط مما تستورده من سجله.
              </Text>
            ) : null}
            <Row label="الصفقات المحفوظة" value={String(trader.trades)} />
            {trader.drafts ? (
              <Row label="مسودات ناقصة الأوقات" value={String(trader.drafts)} color={colors.warn} />
            ) : null}
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
              <Body>استيراد سجل صفقاته: ملف CSV من Binance، أو صورة من سجل المراكز، أو الصق النص.</Body>
              <View style={styles.actions}>
                <Pressable accessibilityRole="button" onPress={() => importFile(trader.trader_id)} style={styles.small}>
                  <Text style={styles.smallText}>📄 اختر ملف CSV</Text>
                </Pressable>
                <Pressable accessibilityRole="button" onPress={() => importImage(trader.trader_id)} style={styles.small}>
                  <Text style={styles.smallText}>🖼 صورة سجل المراكز</Text>
                </Pressable>
              </View>
              <View style={styles.actions}>
                {(['device', 'utc'] as const).map((z) => (
                  <Pressable
                    key={z}
                    accessibilityRole="button"
                    accessibilityState={{ selected: zone === z }}
                    onPress={() => setZone(z)}
                    style={[styles.small, zone === z && { backgroundColor: colors.accent }]}>
                    <Text style={styles.smallText}>
                      {z === 'device' ? `أوقات بتوقيت جهازي (${offsetLabel(deviceOffsetMinutes())})` : 'أوقات UTC'}
                    </Text>
                  </Pressable>
                ))}
              </View>
              <Body color={colors.muted}>
                ملفات Binance المكتوب فيها UTC تُقرأ بتوقيت UTC تلقائيًا. الصور تُقرأ بتوقيت جهازك. ما لا يُقرأ بوضوح يبقى ناقصًا ولا
                يُخمَّن، وتكتمل المسودات تلقائيًا عند وصول نفس الصفقة كاملة.
              </Body>
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
                سطر لكل صفقة مغلقة. الخانات غير المعروفة اتركها فارغة، ولا تُخمَّن.
              </Body>
              <Button label="استورد وحلل" disabled={!csv.trim()} onPress={() => importCsv(trader.trader_id)} />
              {preview && preview.trader === trader.trader_id ? (
                <View style={styles.reviewBox}>
                  <Body>ما قُرئ من الصورة (للمراجعة قبل الحفظ):</Body>
                  {[...preview.trades, ...preview.drafts].map((t, k) => (
                    <Text key={k} style={styles.rankLine}>
                      {t.symbol} {t.side === 'BUY' ? 'شراء' : 'بيع'}
                      {t.leverage ? ` ×${t.leverage}` : ''}، دخول {t.entry_price ?? '—'}، خروج {t.exit_price ?? '—'}، فتح{' '}
                      {t.opened_ms ? when(t.opened_ms) : '—'}، إغلاق {t.closed_ms ? when(t.closed_ms) : '—'}، كمية{' '}
                      {t.qty ?? '—'}، ربح {t.pnl ?? '—'}، رسوم {t.fee ?? '—'}
                    </Text>
                  ))}
                  {preview.trades.length + preview.drafts.length === 0 ? (
                    <Body color={colors.warn}>لم تُقرأ أي صفقة بوضوح من هذه الصورة.</Body>
                  ) : null}
                  <ImportReport rows={preview.rows} />
                  <View style={styles.actions}>
                    <Pressable accessibilityRole="button" onPress={confirmPreview} style={[styles.small, styles.approveBg]}>
                      <Text style={styles.smallText}>اعتمد واحفظ</Text>
                    </Pressable>
                    <Pressable accessibilityRole="button" onPress={() => setPreview(null)} style={styles.small}>
                      <Text style={styles.smallText}>إلغاء</Text>
                    </Pressable>
                  </View>
                </View>
              ) : null}
              <ImportReport rows={rows} />
              {ocrText ? (
                <View>
                  <Body color={colors.muted}>النص الذي قُرئ من الصورة (للمراجعة):</Body>
                  <Text style={styles.ocr}>{ocrText}</Text>
                </View>
              ) : null}
              <Drafts drafts={detail.drafts} onSave={saveDraft} />
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
            <Row label="المفتاح (لا يُعرض بعد الحفظ)" value={account.key_hint ?? ''} />
            <Row label="مصدره" value={account.source === 'env' ? 'ملف الإعدادات على الخادم (.env)' : 'التطبيق'} />
            <Row
              label="مقيّد بعنوان الخادم"
              value={account.ip_restricted ? 'نعم' : 'لا'}
              color={account.ip_restricted ? colors.gain : colors.warn}
            />
            {!account.ip_restricted ? (
              <Body color={colors.warn}>
                للأمان: في Binance افتح إدارة API، ثم «تعديل القيود»، واختر «Restrict access to trusted IPs only» وأضف{' '}
                {account.server_ip ?? 'عنوان الخادم'}، ثم اربط المفتاح من جديد هنا.
              </Body>
            ) : null}
            <Row label="آخر مزامنة" value={account.synced_ms ? when(account.synced_ms) : 'تبدأ الآن تلقائيًا'} />
            <Row label="المزامنة التلقائية" value={`كل ${account.every_hours} ساعات`} />
            <Button label="اختبر الاتصال" color={colors.line} onPress={testAccount} />
            {tested ? (
              <>
                <Row label="رصيد محفظة العقود" value={`${tested.balance.wallet.toFixed(2)} USDT`} />
                <Row label="المتاح" value={`${tested.balance.available.toFixed(2)} USDT`} />
                <Row label="سجلات آخر 30 يومًا" value={String(tested.income_30d)} />
                <Row
                  label="عملات تداولت عليها"
                  value={tested.symbols_30d.map((x) => x.replace('USDT', '')).join('، ') || '—'}
                />
                <Row label="صفقات محفوظة من حسابك" value={String(tested.synced_trades)} />
              </>
            ) : null}
            <Button label="زامن الآن إلى «صفقات حسابي»" onPress={sync} />
            {account.source !== 'env' ? (
              <Button
                label="فك الربط"
                color={colors.line}
                onPress={async () => {
                  setAccount(await call<Account>('/account', 'DELETE'));
                }}
              />
            ) : null}
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
  reviewBox: { borderWidth: 1, borderColor: colors.warn, borderRadius: 10, padding: 10, gap: 6 },
  approveBg: { backgroundColor: colors.gain },
  ocr: {
    color: colors.muted,
    fontSize: 11,
    lineHeight: 16,
    writingDirection: 'ltr',
    textAlign: 'left',
    borderWidth: 1,
    borderColor: colors.line,
    borderRadius: 8,
    padding: 8,
  },
  approve: {
    alignSelf: 'flex-start',
    backgroundColor: colors.gain,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 6,
    marginTop: 4,
  },
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
