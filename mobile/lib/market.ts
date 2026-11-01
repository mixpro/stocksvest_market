// Company names and market hours from the market service (through the app's /v1/market routes).

import { useEffect, useState } from 'react';

import { useApi, useApp } from './app';

/** "Agilent Technologies, Inc. Common Stock" → "Agilent Technologies, Inc." */
export const shortName = (name: string) =>
  name.replace(/\s+(Class [A-Z]\s+)?(Common Stock|Common Shares|Ordinary Shares|American Depositary|Depositary Shares|ADS)\b.*$/i, '').trim();

let names: Promise<Record<string, string>> | null = null; // one call per app start (~880 symbols)

/** ticker → short company name (empty until loaded). */
export function useNames(): Record<string, string> {
  const { api } = useApp();
  const [map, setMap] = useState<Record<string, string>>({});
  useEffect(() => {
    names ??= api<{ symbols: { ticker: string; name: string }[] }>('/v1/market/symbols?limit=50000')
      .then((r) => Object.fromEntries(r.symbols.map((s) => [s.ticker, shortName(s.name)])))
      .catch((e) => {
        names = null; // try again next time
        throw e;
      });
    names.then(setMap, () => {});
  }, [api]);
  return map;
}

type Day = { date: string; preMarketOpen: string; regularOpen: string; regularClose: string; afterHoursClose: string };

const hm = (iso: string) => iso.slice(11, 16); // calendar times are New York time with their offset
const nyDate = (d: Date) => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(d);
const weekday = (date: string) => new Date(`${date}T12:00:00Z`).toLocaleDateString('en-US', { weekday: 'short', timeZone: 'UTC' });

/** "market open until 16:00 ET", "after hours until 20:00 ET", "market closed · opens Mon 09:30 ET"... */
export function marketStatus(days: Day[], now = new Date()): string | null {
  const n = now.getTime(), t = (s: string) => new Date(s).getTime();
  for (const d of days) {
    if (n >= t(d.afterHoursClose)) continue;
    if (n >= t(d.regularClose)) return `after hours until ${hm(d.afterHoursClose)} ET`;
    if (n >= t(d.regularOpen)) return `market open until ${hm(d.regularClose)} ET`;
    if (n >= t(d.preMarketOpen)) return `pre-market · opens ${hm(d.regularOpen)} ET`;
    return `market closed · opens ${d.date === nyDate(now) ? '' : `${weekday(d.date)} `}${hm(d.regularOpen)} ET`;
  }
  return null;
}

/** The market's state now, from the trading calendar (the next 30 days). */
export function useMarketStatus() {
  const cal = useApi<{ days: Day[] }>('/v1/market/calendar');
  return { status: cal.data ? marketStatus(cal.data.days) : null, reload: cal.reload };
}
