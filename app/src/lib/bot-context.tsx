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
  loadProfiles,
  type Place,
  type Profiles,
  request,
  saveProfiles,
  type Status,
} from '@/lib/api';

const POLL_MS = 5000;

type Bot = {
  ready: boolean;
  profiles: Profiles;
  switchTo: (place: Place) => Promise<void>;
  connection: Connection | null;
  status: Status | null;
  online: boolean;
  error: string;
  connect: (connection: Connection, place: Place) => Promise<void>;
  call: <T>(path: string, method?: string, body?: unknown, timeoutMs?: number) => Promise<T>;
  refresh: () => Promise<void>;
};

const BotContext = createContext<Bot | null>(null);

export function errorCode(error: unknown): string {
  return error instanceof ApiError ? error.message : 'offline';
}

export function BotProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [profiles, setProfiles] = useState<Profiles>({ active: 'laptop' });
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
    loadProfiles().then(async (saved) => {
      // Opened by the laptop as http://<server>/#token=...: connect to this server at once.
      // The part after # never leaves the browser, and it is removed from the address.
      if (typeof window !== 'undefined' && window.location?.hash.startsWith('#token=')) {
        const token = decodeURIComponent(window.location.hash.slice('#token='.length));
        const clean = () => window.history.replaceState(null, '', window.location.pathname);
        clean();
        setTimeout(clean, 1500); // the router may put the address back while it starts
        saved = { ...saved, laptop: { url: window.location.origin, token }, active: 'laptop' };
        await saveProfiles(saved);
      }
      const active = saved[saved.active] ?? null;
      current.current = active;
      setProfiles(saved);
      setConnection(active);
      setReady(true);
      refresh();
    });
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const connect = useCallback(
    async (next: Connection, place: Place) => {
      await request<Status>(next, '/status');
      const updated: Profiles = { ...profiles, [place]: next, active: place };
      await saveProfiles(updated);
      setProfiles(updated);
      current.current = next;
      setConnection(next);
      setStatus(null);
      await refresh();
    },
    [profiles, refresh]
  );

  const switchTo = useCallback(
    async (place: Place) => {
      const target = profiles[place];
      if (!target) return;
      const updated: Profiles = { ...profiles, active: place };
      await saveProfiles(updated);
      setProfiles(updated);
      current.current = target;
      setConnection(target);
      setStatus(null);
      await refresh();
    },
    [profiles, refresh]
  );

  const call = useCallback(
    async <T,>(path: string, method = 'POST', body?: unknown, timeoutMs?: number) => {
      const active = current.current;
      if (!active) throw new ApiError('not_connected', 0);
      const result = await request<T>(active, path, { method, body, timeoutMs });
      await refresh();
      return result;
    },
    [refresh]
  );

  const value = useMemo(
    () => ({ ready, profiles, switchTo, connection, status, online, error, connect, call, refresh }),
    [ready, profiles, switchTo, connection, status, online, error, connect, call, refresh]
  );
  return <BotContext.Provider value={value}>{children}</BotContext.Provider>;
}

export function useBot(): Bot {
  const bot = useContext(BotContext);
  if (!bot) throw new Error('useBot must be used inside BotProvider');
  return bot;
}
