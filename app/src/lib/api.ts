import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

export type Limits = {
  capital_usdt: number;
  max_daily_loss_usdt: number;
  leverage: number;
  risk_per_trade_pct: number;
  stop_loss_pct: number;
  take_profit_pct: number;
  symbols: string[];
};

export type Market = { symbol: string; price: number; min_order_usdt: number };

export type Trade = {
  id: number;
  symbol: string;
  side: 'BUY' | 'SELL';
  qty: number;
  entry_price: number;
  stop_price: number;
  target_price: number;
  opened_ms: number;
  closed_ms: number | null;
  exit_price: number | null;
  pnl: number | null;
  fees: number | null;
};

export type BotEvent = { id: number; at_ms: number; level: string; code: string; detail: string };

export type Status = {
  mode: 'paper' | 'demo';
  strategy: string;
  strategy_proven: boolean;
  running: boolean;
  halted: string;
  balance: number;
  unrealized: number;
  realized_today: number;
  positions: Record<string, { qty: number; mark: number; unrealized: number }>;
  open_trades: Trade[];
  limits: Limits;
  budget_left: number;
  max_order_usdt: number;
  minimums: Record<string, number>;
  last_tick_ms: number;
  last_error: string;
};

export type Connection = { url: string; token: string };

const STORAGE_KEY = 'sonic.connection';

export async function loadConnection(): Promise<Connection | null> {
  const raw =
    Platform.OS === 'web'
      ? globalThis.localStorage?.getItem(STORAGE_KEY)
      : await SecureStore.getItemAsync(STORAGE_KEY);
  return raw ? (JSON.parse(raw) as Connection) : null;
}

export async function saveConnection(connection: Connection): Promise<void> {
  const raw = JSON.stringify(connection);
  if (Platform.OS === 'web') {
    globalThis.localStorage?.setItem(STORAGE_KEY, raw);
  } else {
    await SecureStore.setItemAsync(STORAGE_KEY, raw);
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
  }
}

export async function request<T>(
  connection: Connection,
  path: string,
  options: { method?: string; body?: unknown } = {}
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(`${connection.url.replace(/\/+$/, '')}${path}`, {
      method: options.method ?? 'GET',
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${connection.token}`,
        'Content-Type': 'application/json',
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : 'invalid_values';
      throw new ApiError(detail, response.status);
    }
    return data as T;
  } finally {
    clearTimeout(timer);
  }
}
