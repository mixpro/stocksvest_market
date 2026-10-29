import { useCallback, useEffect, useState } from 'react';
import { ScrollView, View } from 'react-native';

import { Card, ErrorNote, money, pct, Screen, Segmented, signedMoney, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import { cardColors } from '@/lib/theme';

type Env = 'ALL' | 'PAPER' | 'LIVE';
type Portfolio = {
  id: string; name: string; brokerAccountId: string; equity: number; allocated: number;
  totalReturnPct: number; positions: unknown[];
};

export default function Home() {
  const { api, colors } = useApp();
  const [env, setEnv] = useState<Env>('ALL');
  const [portfolios, setPortfolios] = useState<(Portfolio & { env: string })[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const automations = useApi<{ automations: { status: string }[] }>('/v1/automations');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [{ accounts }, list] = await Promise.all([
        api<{ accounts: { id: string; environment: string }[] }>('/v1/broker-accounts'),
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
    load();
  }, [load]);

  const shown = portfolios.filter((p) => env === 'ALL' || p.env === env);
  const equity = shown.reduce((s, p) => s + p.equity, 0);
  const allocated = shown.reduce((s, p) => s + p.allocated, 0);
  const gain = equity - allocated;
  const autos = automations.data?.automations ?? [];
  const active = autos.filter((a) => a.status === 'ACTIVE').length;

  return (
    <Screen onRefresh={() => { load(); automations.reload(); }} refreshing={loading}>
      <T size={13} color={colors.muted}>StocksVest</T>
      <Segmented<Env> value={env} onChange={setEnv}
        options={[{ value: 'ALL', label: 'All' }, { value: 'PAPER', label: 'Paper' }, { value: 'LIVE', label: 'Live' }]} />
      <ErrorNote error={error} />

      <View style={{ alignItems: 'center', gap: 6, marginTop: 8 }}>
        <T size={13} color={colors.muted}>Total equity</T>
        <T size={38} weight="heavy" style={{ letterSpacing: -1 }}>{money(equity)}</T>
        {allocated > 0 ? (
          <View style={{ paddingHorizontal: 12, paddingVertical: 5, borderRadius: 999,
            backgroundColor: gain >= 0 ? colors.upSoft : colors.downSoft }}>
            <T size={13} weight="bold" color={gain >= 0 ? colors.up : colors.down}>
              {signedMoney(gain)} · {pct((gain / allocated) * 100)} total return
            </T>
          </View>
        ) : null}
      </View>

      <T size={18} weight="heavy" style={{ marginTop: 12 }}>Portfolios</T>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ marginHorizontal: -20 }}
        contentContainerStyle={{ paddingHorizontal: 20, gap: 12 }}>
        {shown.map((p, i) => (
          <View key={p.id} style={{ width: 168, height: 128, padding: 16, borderRadius: 22, justifyContent: 'space-between',
            backgroundColor: cardColors[i % cardColors.length][1] }}>
            <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 6 }}>
              <T size={13} weight="semibold" color="#FFFFFF" style={{ flexShrink: 1 }}>{p.name}</T>
              <T size={10} weight="bold" color="#FFFFFF">{p.env}</T>
            </View>
            <View>
              <T size={20} weight="heavy" color="#FFFFFF">{money(p.equity)}</T>
              <T size={12} weight="semibold" color="#FFFFFF">
                {pct(p.totalReturnPct)} · {p.positions.length ? `${p.positions.length} stocks` : 'cash'}
              </T>
            </View>
          </View>
        ))}
      </ScrollView>

      <Card style={{ marginTop: 4 }}>
        <T weight="bold">{active} automations running</T>
        <T size={12} color={colors.muted}>{autos.length - active} paused</T>
      </Card>
    </Screen>
  );
}
