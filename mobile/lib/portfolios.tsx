// Every portfolio with its full summary and whether its broker account is PAPER or LIVE.

import { Link } from 'expo-router';
import { useCallback, useEffect, useState } from 'react';
import { Pressable, View } from 'react-native';

import { money, pct, T } from '@/components/ui';
import { useApp } from '@/lib/app';
import { cardColors } from '@/lib/theme';

export type Position = {
  symbol: string; budgetId: string | null; quantity: number; averageCost: number; costBasis: number;
  price: number | null; marketValue: number | null; unrealizedPnL: number | null; unrealizedPct: number | null;
};
export type Portfolio = {
  id: string; name: string; brokerAccountId: string; env: 'PAPER' | 'LIVE'; equity: number; allocated: number; cash: number;
  positionsValue: number; unrealizedPnL: number; realizedPnL: number; totalReturn: number; totalReturnPct: number;
  positions: Position[]; budgets: { id: string; name: string }[];
};

export function usePortfolios() {
  const { api } = useApp();
  const [portfolios, setPortfolios] = useState<Portfolio[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [{ accounts }, list] = await Promise.all([
        api<{ accounts: { id: string; environment: Portfolio['env'] }[] }>('/v1/broker-accounts'),
        api<{ portfolios: { id: string }[] }>('/v1/portfolios'),
      ]);
      const envOf = Object.fromEntries(accounts.map((a) => [a.id, a.environment]));
      const full = await Promise.all(list.portfolios.map((p) => api<Portfolio>(`/v1/portfolios/${p.id}`)));
      setPortfolios(full.map((p) => ({ ...p, env: envOf[p.brokerAccountId] })).sort((a, b) => b.equity - a.equity));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [api]);
  useEffect(() => {
    reload();
  }, [reload]);
  return { portfolios, error, loading, reload };
}

export function PortfolioCard({ p, index, wide }: { p: Portfolio; index: number; wide?: boolean }) {
  return (
    <Link href={{ pathname: '/portfolio/[id]', params: { id: p.id } }} asChild>
      <Pressable accessibilityRole="button" style={{ width: wide ? undefined : 168, height: wide ? 110 : 128, padding: 16,
        borderRadius: 22, justifyContent: 'space-between', backgroundColor: cardColors[index % cardColors.length][1] }}>
        <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
          <T size={13} weight="semibold" color="#FFFFFF" style={{ flexShrink: 1 }}>{p.name}</T>
          <View style={{ paddingHorizontal: 7, paddingVertical: 2, borderRadius: 6,
            backgroundColor: p.env === 'LIVE' ? '#FFFFFF' : 'rgba(255,255,255,0.22)' }}>
            <T size={10} weight="bold" color={p.env === 'LIVE' ? '#0B6B59' : '#FFFFFF'}>{p.env}</T>
          </View>
        </View>
        <View>
          <T size={20} weight="heavy" color="#FFFFFF">{money(p.equity)}</T>
          <T size={12} weight="semibold" color="#FFFFFF">
            {pct(p.totalReturnPct)} · {p.positions.length ? `${p.positions.length} stocks` : 'cash'}
          </T>
        </View>
      </Pressable>
    </Link>
  );
}
