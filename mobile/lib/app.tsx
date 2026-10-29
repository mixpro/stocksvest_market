// App-wide state: the server address, the API key (both kept in the phone's secure store) and the theme choice.

import * as SecureStore from 'expo-secure-store';
import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from 'react';
import { useColorScheme } from 'react-native';

import { dark, light, Palette } from './theme';

export type ThemePref = 'light' | 'dark' | 'system';

type App = {
  ready: boolean;
  serverUrl: string;
  apiKey: string;
  themePref: ThemePref;
  colors: Palette;
  scheme: 'light' | 'dark';
  save: (changes: Partial<{ serverUrl: string; apiKey: string; themePref: ThemePref }>) => Promise<void>;
  api: <T = any>(path: string, init?: RequestInit) => Promise<T>;
};

const DEFAULT_URL = 'http://100.126.3.52:8100'; // the app service on tinkergw, over Tailscale
const KEYS = { serverUrl: 'serverUrl', apiKey: 'apiKey', themePref: 'themePref' } as const;

const Ctx = createContext<App | null>(null);

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function AppProvider({ children }: { children: ReactNode }) {
  const system = useColorScheme();
  const [ready, setReady] = useState(false);
  const [values, setValues] = useState({ serverUrl: DEFAULT_URL, apiKey: '', themePref: 'system' as ThemePref });

  useEffect(() => {
    Promise.all(Object.values(KEYS).map((k) => SecureStore.getItemAsync(k))).then(([url, key, theme]) => {
      setValues({ serverUrl: url || DEFAULT_URL, apiKey: key || '', themePref: (theme as ThemePref) || 'system' });
      setReady(true);
    });
  }, []);

  const save: App['save'] = useCallback(async (changes) => {
    for (const [k, v] of Object.entries(changes)) await SecureStore.setItemAsync(k, String(v));
    setValues((old) => ({ ...old, ...changes }));
  }, []);

  const api: App['api'] = useCallback(
    async (path, init) => {
      const res = await fetch(values.serverUrl.replace(/\/$/, '') + path, {
        ...init,
        headers: { 'X-API-Key': values.apiKey, 'Content-Type': 'application/json', ...init?.headers },
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new ApiError(res.status, body?.detail ? String(body.detail) : `HTTP ${res.status}`);
      return body;
    },
    [values.serverUrl, values.apiKey],
  );

  const scheme = values.themePref === 'system' ? (system === 'dark' ? 'dark' : 'light') : values.themePref;
  return (
    <Ctx.Provider value={{ ready, ...values, colors: scheme === 'dark' ? dark : light, scheme, save, api }}>
      {children}
    </Ctx.Provider>
  );
}

export function useApp(): App {
  const app = useContext(Ctx);
  if (!app) throw new Error('useApp outside AppProvider');
  return app;
}

/** Load `path` from the API; `reload` fetches again (pull to refresh). */
export function useApi<T = any>(path: string | null) {
  const { api } = useApp();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [api, path]);
  useEffect(() => {
    reload();
  }, [reload]);
  return { data, error, loading, reload };
}
