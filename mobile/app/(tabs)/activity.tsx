import { useCallback, useEffect, useState } from 'react';
import { View } from 'react-native';

import { Card, ErrorNote, money, Pills, Screen, T } from '@/components/ui';
import { useApp } from '@/lib/app';

type Entry = {
  id: number; at: string; action: string; entityId: string | null; related: Record<string, string>;
  error: string | null; details: Record<string, any>;
};
type Kind = 'Orders' | 'Alerts' | 'Money';
type Item = { id: number; at: Date; kind: Kind; title: string; sub: string; amount?: number; badge: string };

// The audit log records everything; the feed shows only what a person cares about.
const ACTIONS: Record<string, Kind> = {
  FILL_IMPORTED: 'Orders', ORDER_EXPIRED: 'Orders', ORDER_REJECTED: 'Orders',
  KILL_RULE: 'Alerts', KILL_ALL: 'Alerts', BROKER_MISMATCH: 'Alerts', BROKER_SYNC_FAILED: 'Alerts',
  AUTOMATION_RUNS_MISSED: 'Alerts',
  ACCOUNT_FEE: 'Money', DIVIDEND: 'Money', PORTFOLIO_CAPITAL_ALLOCATE: 'Money', PORTFOLIO_CAPITAL_WITHDRAW: 'Money',
};

function describe(e: Entry, names: Record<string, string>): Omit<Item, 'id' | 'at' | 'kind'> {
  const d = e.details ?? {};
  const pf = names[e.related?.portfolioId ?? e.entityId ?? ''] ?? '';
  switch (e.action) {
    case 'FILL_IMPORTED': {
      const qty = +d.quantity, price = +d.price, sell = d.side === 'SELL';
      return { badge: d.side, title: `${sell ? 'Sold' : 'Bought'} ${+qty.toFixed(4)} ${d.symbol}`,
        sub: `${pf} · at ${money(price)}`, amount: (sell ? 1 : -1) * qty * price };
    }
    case 'ORDER_EXPIRED':
    case 'ORDER_REJECTED':
      return { badge: '!', title: `${d.side === 'SELL' ? 'Sell' : 'Buy'} ${d.symbol} ${e.action === 'ORDER_EXPIRED' ? 'expired' : 'rejected'}`,
        sub: e.error ?? pf };
    case 'KILL_RULE':
      return { badge: '!', title: 'Kill rule paused buys', sub: `${pf} · ${d.drawdownPercent}% below peak` };
    case 'KILL_ALL':
      return { badge: '!', title: 'Kill all', sub: `${d.sells} sells placed, ${d.cancelled} buys cancelled` };
    case 'BROKER_MISMATCH':
      return { badge: '!', title: 'App and broker disagree', sub: e.error ?? '' };
    case 'BROKER_SYNC_FAILED':
      return { badge: '!', title: 'Broker sync failed', sub: (e.error ?? '').slice(0, 80) };
    case 'AUTOMATION_RUNS_MISSED':
      return { badge: '!', title: `${d.count} automation run(s) missed`, sub: 'The app was down at the scheduled time' };
    case 'ACCOUNT_FEE':
      return { badge: '$', title: 'Broker fee', sub: d.description ?? '', amount: -d.amount };
    case 'DIVIDEND':
      return { badge: '$', title: `Dividend ${d.symbol}`, sub: d.kind ?? '', amount: +d.amount };
    default: {
      const out = e.action === 'PORTFOLIO_CAPITAL_WITHDRAW';
      return { badge: '$', title: out ? 'Money taken out' : 'Money put in', sub: pf,
        amount: (out ? -1 : 1) * (d.request?.amount ?? 0) };
    }
  }
}

export default function Activity() {
  const { api, colors } = useApp();
  const [items, setItems] = useState<Item[]>([]);
  const [filter, setFilter] = useState<'All' | Kind>('All');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [{ portfolios }, ...lists] = await Promise.all([
        api<{ portfolios: { id: string; name: string }[] }>('/v1/portfolios'),
        ...Object.keys(ACTIONS).map((a) => api<{ entries: Entry[] }>(`/v1/activity?action=${a}&limit=30`)),
      ]);
      const names = Object.fromEntries(portfolios.map((p) => [p.id, p.name]));
      setItems(lists.flatMap((l) => l.entries)
        .map((e) => ({ id: e.id, at: new Date(e.at), kind: ACTIONS[e.action], ...describe(e, names) }))
        .sort((a, b) => b.id - a.id).slice(0, 80));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [api]);
  useEffect(() => {
    load();
  }, [load]);

  const shown = items.filter((i) => filter === 'All' || i.kind === filter);
  const days: [string, Item[]][] = [];
  for (const i of shown) {
    const day = i.at.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    if (days.at(-1)?.[0] !== day) days.push([day, []]);
    days.at(-1)![1].push(i);
  }
  const badge = (i: Item) =>
    i.badge === 'SELL' ? [colors.downSoft, colors.down] : i.badge === 'BUY' ? [colors.accentSoft, colors.accent]
      : i.badge === '$' ? [colors.upSoft, colors.up] : [colors.dangerSoft, colors.danger];

  return (
    <Screen title="Activity" onRefresh={load} refreshing={loading}>
      <Pills options={['All', 'Orders', 'Alerts', 'Money'] as const} value={filter} onChange={setFilter} />
      <ErrorNote error={error} />
      {days.map(([day, list]) => (
        <View key={day} style={{ gap: 8 }}>
          <T size={12} weight="bold" color={colors.muted}>{day.toUpperCase()}</T>
          <Card style={{ paddingVertical: 0 }}>
            {list.map((i, n) => {
              const [bg, fg] = badge(i);
              return (
                <View key={i.id} style={{ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 62, paddingVertical: 10,
                  borderTopWidth: n ? 1 : 0, borderTopColor: colors.line }}>
                  <View style={{ width: 40, height: 38, borderRadius: 12, backgroundColor: bg, alignItems: 'center', justifyContent: 'center' }}>
                    <T size={10} weight="heavy" color={fg}>{i.badge}</T>
                  </View>
                  <View style={{ flex: 1 }}>
                    <T weight="bold">{i.title}</T>
                    {i.sub ? <T size={12} color={colors.muted}>{i.sub}</T> : null}
                  </View>
                  <View style={{ alignItems: 'flex-end' }}>
                    {i.amount != null ? (
                      <T weight="bold" color={i.amount >= 0 ? colors.up : colors.text}>{(i.amount >= 0 ? '+' : '') + money(i.amount)}</T>
                    ) : null}
                    <T size={11} color={colors.muted}>{i.at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</T>
                  </View>
                </View>
              );
            })}
          </Card>
        </View>
      ))}
    </Screen>
  );
}
