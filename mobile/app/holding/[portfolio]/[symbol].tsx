import { Link, useLocalSearchParams } from 'expo-router';
import { useState } from 'react';
import { Pressable, View } from 'react-native';

import { LineChart, Range, RANGES, sinceDays } from '@/components/LineChart';
import { Card, ErrorNote, money, pct, Pills, Screen, signedMoney, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import type { Portfolio } from '@/lib/portfolios';
import { cardColors, gradient } from '@/lib/theme';

const TIMEFRAME: Record<Range, string> = { '1W': '1Hour', '1M': '1Day', '3M': '1Day', '1Y': '1Day', All: '1Week' };

export default function HoldingScreen() {
  const { portfolio, symbol } = useLocalSearchParams<{ portfolio: string; symbol: string }>();
  const { colors } = useApp();
  const [range, setRange] = useState<Range>('1M');
  const p = useApi<Portfolio>(`/v1/portfolios/${portfolio}`);
  const info = useApi<{ name: string; exchange: string; sector: string | null; halalCompliant: boolean }>(
    `/v1/market/symbols/${symbol}`);
  const start = sinceDays(Object.fromEntries(RANGES)[range]);
  const bars = useApi<{ bars: { t: string; c: number }[] }>(
    `/v1/market/bars/${symbol}?timeframe=${TIMEFRAME[range]}${start ? `&start=${start}` : ''}`);

  // The same stock can be held by several budgets and as manual shares: add them up.
  const lots = p.data?.positions.filter((x) => x.symbol === symbol) ?? [];
  const shares = lots.reduce((s, x) => s + x.quantity, 0);
  const cost = lots.reduce((s, x) => s + x.costBasis, 0);
  const value = lots.reduce((s, x) => s + (x.marketValue ?? 0), 0);
  const avg = shares ? cost / shares : 0;
  const price = lots[0]?.price ?? bars.data?.bars.at(-1)?.c ?? null;
  const gain = value - cost;
  const owners = lots.map((x) => p.data?.budgets.find((b) => b.id === x.budgetId)?.name ?? 'Manual');

  const stats: [string, string, string?][] = [
    ['Shares', String(+shares.toFixed(4))],
    ['Average cost', money(avg)],
    ['Market value', money(value)],
    ['Unrealized', signedMoney(gain), gain >= 0 ? colors.up : colors.down],
  ];

  return (
    <Screen back title={symbol} subtitle={p.data?.name} onRefresh={() => { p.reload(); bars.reload(); }}
      refreshing={p.loading || bars.loading}
      footer={(['SELL', 'BUY'] as const).map((side) => (
        <Link key={side} asChild href={{ pathname: '/order', params: { portfolio, symbol, side } }}>
          <Pressable accessibilityRole="button" disabled={side === 'SELL' && !shares}
            style={{ flex: 1, height: 56, borderRadius: 18, alignItems: 'center', justifyContent: 'center',
              backgroundColor: side === 'BUY' ? colors.accent : colors.card, borderWidth: side === 'SELL' ? 2 : 0,
              ...(side === 'BUY' ? { backgroundImage: gradient(cardColors[0]), boxShadow: '0 10px 22px rgba(80, 70, 229, 0.3)' } : {}),
              borderColor: colors.text, opacity: side === 'SELL' && !shares ? 0.4 : 1 }}>
            <T size={16} weight="heavy" color={side === 'BUY' ? '#FFFFFF' : colors.text}>{side === 'BUY' ? 'Buy more' : 'Sell'}</T>
          </Pressable>
        </Link>
      ))}>
      <ErrorNote error={p.error ?? bars.error} />
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
        <View style={{ flex: 1 }}>
          <T size={20} weight="heavy">{info.data?.name ?? symbol}</T>
          <T size={13} color={colors.muted}>{[info.data?.exchange, info.data?.sector].filter(Boolean).join(' · ')}</T>
        </View>
        {info.data ? (
          <View style={{ paddingHorizontal: 12, paddingVertical: 6, borderRadius: 999,
            backgroundColor: info.data.halalCompliant ? colors.upSoft : colors.downSoft }}>
            <T size={12} weight="bold" color={info.data.halalCompliant ? colors.up : colors.down}>
              {info.data.halalCompliant ? '✓ Halal' : 'Not halal'}
            </T>
          </View>
        ) : null}
      </View>

      <View style={{ flexDirection: 'row', alignItems: 'baseline', gap: 10 }}>
        <T size={34} weight="heavy" style={{ letterSpacing: -1 }}>{money(price)}</T>
        {cost ? <T size={14} weight="bold" color={gain >= 0 ? colors.up : colors.down}>{pct((gain / cost) * 100)} on your cost</T> : null}
      </View>

      <Card style={{ gap: 10 }}>
        <LineChart points={(bars.data?.bars ?? []).map((b) => ({ at: b.t, value: b.c }))} height={170}
          color={(price ?? 0) >= avg ? colors.up : colors.down} refLine={avg || undefined} format={money} />
        {avg ? <T size={12} color={colors.muted}>Dashed line: your average cost</T> : null}
        <Pills options={RANGES.map((r) => r[0])} value={range} onChange={setRange} />
      </Card>

      <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 10 }}>
        {stats.map(([label, text, color]) => (
          <Card key={label} style={{ width: '47%', flexGrow: 1, paddingVertical: 12 }}>
            <T size={12} color={colors.muted}>{label}</T>
            <T size={16} weight="heavy" color={color}>{text}</T>
          </Card>
        ))}
      </View>
      {owners.length ? <Card><T size={13}>Owned by <T size={13} weight="bold">{owners.join(', ')}</T></T></Card> : null}
    </Screen>
  );
}
