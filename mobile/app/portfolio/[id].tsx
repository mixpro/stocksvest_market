import { Link, useLocalSearchParams } from 'expo-router';
import { useState } from 'react';
import { Pressable, View } from 'react-native';

import { LineChart, Range, RANGES, sinceDays } from '@/components/LineChart';
import { Card, ErrorNote, money, pct, Pills, Screen, Section, signedMoney, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import type { Portfolio } from '@/lib/portfolios';
import { cardColors } from '@/lib/theme';

type HistoryPoint = { at: string; equity: number; cash: number; totalReturnPct: number };

function useHistory(id: string, range: Range, closesOnly: boolean) {
  const start = sinceDays(Object.fromEntries(RANGES)[range]);
  const q = new URLSearchParams({ ...(start ? { start } : {}), ...(closesOnly ? { kind: 'CLOSE' } : {}) });
  const { data } = useApi<{ points: HistoryPoint[]; current: HistoryPoint }>(`/v1/portfolios/${id}/history?${q}`);
  return data ? [...data.points, data.current] : [];
}

export default function PortfolioScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { colors } = useApp();
  const { data: p, error, loading, reload } = useApi<Portfolio>(`/v1/portfolios/${id}`);
  const accounts = useApi<{ accounts: { id: string; environment: string; name: string }[] }>('/v1/broker-accounts');
  const [returnRange, setReturnRange] = useState<Range>('3M');
  const [historyRange, setHistoryRange] = useState<Range>('1M');
  // One point per close, except on 1W where every 15-minute snapshot is shown.
  const returns = useHistory(id, returnRange, returnRange !== '1W');
  const history = useHistory(id, historyRange, false);

  if (!p) return <Screen back title="Portfolio"><ErrorNote error={error} /></Screen>;
  const account = accounts.data?.accounts.find((a) => a.id === p.brokerAccountId);
  const gain = p.equity - p.allocated;
  const tiles: [string, number][] = [
    ['Invested', p.positionsValue], ['Cash', p.cash], ['Unrealized', p.unrealizedPnL], ['Realized', p.realizedPnL],
  ];
  const positions = [...p.positions].sort((a, b) => (b.marketValue ?? 0) - (a.marketValue ?? 0));

  return (
    <Screen back title={p.name} subtitle={account ? `${account.environment} · ${account.name}` : undefined}
      onRefresh={reload} refreshing={loading}>
      <View style={{ alignItems: 'center', gap: 4 }}>
        <T size={34} weight="heavy" style={{ letterSpacing: -1 }}>{money(p.equity)}</T>
        <T size={13} weight="bold" color={gain >= 0 ? colors.up : colors.down}>
          {signedMoney(gain)} · {pct(p.totalReturnPct)} since start
        </T>
      </View>

      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 12 }}>
        {tiles.map(([label, value], i) => (
          <View key={label} style={{ width: '47%', flexGrow: 1, height: 88, padding: 14, borderRadius: 20,
            justifyContent: 'space-between', backgroundColor: [cardColors[0], cardColors[2], cardColors[1], cardColors[3]][i][1] }}>
            <T size={12} weight="semibold" color="#FFFFFF">{label}</T>
            <T size={19} weight="heavy" color="#FFFFFF">{label === 'Unrealized' || label === 'Realized' ? signedMoney(value) : money(value)}</T>
          </View>
        ))}
      </View>

      <Section title="Total return %">
        <LineChart points={returns.map((h) => ({ at: h.at, value: h.totalReturnPct }))} refLine={0}
          color={gain >= 0 ? colors.up : colors.down} format={(v) => pct(v)} />
        <Pills options={RANGES.map((r) => r[0])} value={returnRange} onChange={setReturnRange} />
      </Section>
      <Section title="History: equity and cash" startOpen={false}>
        <LineChart points={history.map((h) => ({ at: h.at, value: h.equity }))} color={colors.accent} format={money} />
        <T size={12} color={colors.muted}>Cash now {money(p.cash)}</T>
        <Pills options={RANGES.map((r) => r[0])} value={historyRange} onChange={setHistoryRange} />
      </Section>

      <T size={17} weight="heavy" style={{ marginTop: 4 }}>Holdings · {positions.length}</T>
      {positions.length ? (
        <Card style={{ paddingVertical: 0 }}>
          {positions.map((x, i) => (
            <Link key={`${x.symbol}:${x.budgetId}`} asChild
              href={{ pathname: '/holding/[portfolio]/[symbol]', params: { portfolio: p.id, symbol: x.symbol } }}>
              <Pressable accessibilityRole="button" style={{ flexDirection: 'row', alignItems: 'center', gap: 12, height: 62,
                borderTopWidth: i ? 1 : 0, borderTopColor: colors.line }}>
                <View style={{ width: 44, height: 38, borderRadius: 12, backgroundColor: colors.accentSoft,
                  alignItems: 'center', justifyContent: 'center' }}>
                  <T size={10} weight="heavy" color={colors.accent}>{x.symbol}</T>
                </View>
                <View style={{ flex: 1 }}>
                  <T weight="bold">{x.symbol}</T>
                  <T size={12} color={colors.muted}>{+x.quantity.toFixed(4)} shares{x.budgetId ? '' : ' · manual'}</T>
                </View>
                <View style={{ alignItems: 'flex-end' }}>
                  <T weight="bold">{money(x.marketValue)}</T>
                  <T size={12} weight="bold" color={(x.unrealizedPct ?? 0) >= 0 ? colors.up : colors.down}>{pct(x.unrealizedPct)}</T>
                </View>
              </Pressable>
            </Link>
          ))}
        </Card>
      ) : (
        <Card><T color={colors.muted}>No holdings, all cash.</T></Card>
      )}
    </Screen>
  );
}
