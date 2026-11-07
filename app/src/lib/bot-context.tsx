import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import {
  ApiError,
  type Connection,
  loadConnection,
  request,
  saveConnection,
  type Status,
} from '@/lib/api';

const POLL_MS = 5000;

type Bot = {
  ready: boolean;
  connection: Connection | null;
  status: Status | null;
  online: boolean;
  error: string;
  connect: (connection: Connection) => Promise<void>;
  call: <T>(path: string, method?: string, body?: unknown) => Promise<T>;
  refresh: () => Promise<void>;
};

const BotContext = createContext<Bot | null>(null);

export function errorCode(error: unknown): string {
  return error instanceof ApiError ? error.message : 'offline';
}

export function BotProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [online, setOnline] = useState(false);
  const [error, setError] = useState('');
  const current = useRef<Connection | null>(null);

  const refresh = useCallback(async () => {
    const active = current.current;
    if (!active) return;
    try {
      setStatus(await request<Status>(active, '/status'));
      setOnline(true);
      setError('');
    } catch (failure) {
      setOnline(false);
      setError(errorCode(failure));
    }
  }, []);

  useEffect(() => {
    loadConnection().then((saved) => {
      current.current = saved;
      setConnection(saved);
      setReady(true);
      refresh();
    });
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const connect = useCallback(
    async (next: Connection) => {
      await request<Status>(next, '/status');
      await saveConnection(next);
      current.current = next;
      setConnection(next);
      await refresh();
    },
    [refresh]
  );

  const call = useCallback(
    async <T,>(path: string, method = 'POST', body?: unknown) => {
      const active = current.current;
      if (!active) throw new ApiError('not_connected', 0);
      const result = await request<T>(active, path, { method, body });
      await refresh();
      return result;
    },
    [refresh]
  );

  const value = useMemo(
    () => ({ ready, connection, status, online, error, connect, call, refresh }),
    [ready, connection, status, online, error, connect, call, refresh]
  );
  return <BotContext.Provider value={value}>{children}</BotContext.Provider>;
}

export function useBot(): Bot {
  const bot = useContext(BotContext);
  if (!bot) throw new Error('useBot must be used inside BotProvider');
  return bot;
}
