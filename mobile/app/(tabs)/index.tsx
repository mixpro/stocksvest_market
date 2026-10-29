import { Link } from 'expo-router';
import { useState } from 'react';
import { Pressable, ScrollView, View } from 'react-native';

import { Card, ErrorNote, money, pct, Screen, Segmented, signedMoney, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import { PortfolioCard, usePortfolios } from '@/lib/portfolios';

type Env = 'ALL' | 'PAPER' | 'LIVE';

export default function Home() {
  const { colors } = useApp();
  const [env, setEnv] = useState<Env>('ALL');
  const { portfolios, error, loading, reload } = usePortfolios();
  const automations = useApi<{ automations: { status: string }[] }>('/v1/automations');

  const shown = portfolios.filter((p) => env === 'ALL' || p.env === env);
  const equity = shown.reduce((s, p) => s + p.equity, 0);
  const allocated = shown.reduce((s, p) => s + p.allocated, 0);
  const gain = equity - allocated;
  const autos = automations.data?.automations ?? [];
  const active = autos.filter((a) => a.status === 'ACTIVE').length;

  return (
    <Screen onRefresh={() => { reload(); automations.reload(); }} refreshing={loading}>
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
        {shown.map((p, i) => <PortfolioCard key={p.id} p={p} index={i} />)}
      </ScrollView>

      <Link href="/automations" asChild>
        <Pressable accessibilityRole="button">
          <Card style={{ marginTop: 4 }}>
            <T weight="bold">{active} automations running</T>
            <T size={12} color={colors.muted}>{autos.length - active} paused</T>
          </Card>
        </Pressable>
      </Link>
    </Screen>
  );
}
