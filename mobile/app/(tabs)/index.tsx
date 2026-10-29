import { Link } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useEffect, useState } from 'react';
import { Pressable, ScrollView, View } from 'react-native';

import { LineChart, Point, Range, RANGES, sinceDays } from '@/components/LineChart';
import { Card, ErrorNote, money, pct, Pills, Screen, Segmented, signedMoney, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import { PortfolioCard, usePortfolios } from '@/lib/portfolios';

type Env = 'ALL' | 'PAPER' | 'LIVE';
type Snap = { at: string; equity: number; allocated: number };

/** Total return % of several portfolios together: at each snapshot time, every portfolio's latest snapshot so far,
 *  added up (equity over capital). One point per close, except 1W (every 15 minutes). */
function useCombinedReturns(ids: string, range: Range): Point[] {
  const { api } = useApp();
  const [points, setPoints] = useState<Point[]>([]);
  useEffect(() => {
    const start = sinceDays(Object.fromEntries(RANGES)[range]);
    const q = new URLSearchParams({ ...(start ? { start } : {}), ...(range !== '1W' ? { kind: 'CLOSE' } : {}) });
    Promise.all(ids.split(',').filter(Boolean).map((id) =>
      api<{ points: Snap[]; current: Snap }>(`/v1/portfolios/${id}/history?${q}`).then((h) => [...h.points, h.current])))
      .then((series) => {
        const times = [...new Set(series.flat().map((s) => s.at))].sort();
        const latest: (Snap | undefined)[] = series.map(() => undefined);
        const idx = series.map(() => 0);
        setPoints(times.map((at) => {
          series.forEach((s, i) => {
            while (idx[i] < s.length && s[idx[i]].at <= at) latest[i] = s[idx[i]++];
          });
          const have = latest.filter(Boolean) as Snap[];
          const eq = have.reduce((a, s) => a + s.equity, 0), cap = have.reduce((a, s) => a + s.allocated, 0);
          return { at, value: cap ? (eq / cap - 1) * 100 : 0 };
        }));
      })
      .catch(() => setPoints([]));
  }, [api, ids, range]);
  return points;
}

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
  const [range, setRange] = useState<Range>('1W');
  const returns = useCombinedReturns(shown.map((p) => p.id).join(','), range);

  return (
    <Screen onRefresh={() => { reload(); automations.reload(); }} refreshing={loading}>
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <T size={22} weight="heavy">StocksVest</T>
        <Link href="/kill-all" asChild>
          <Pressable accessibilityRole="button" accessibilityLabel="Kill all"
            style={{ height: 44, paddingHorizontal: 14, borderRadius: 22, backgroundColor: colors.dangerSoft, flexDirection: 'row',
              alignItems: 'center', gap: 6 }}>
            <SymbolView name={{ ios: 'power', android: 'power_settings_new', web: 'power_settings_new' }} tintColor={colors.danger} size={18} />
            <T size={13} weight="heavy" color={colors.danger}>Kill all</T>
          </Pressable>
        </Link>
      </View>
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

      <Card style={{ gap: 10 }}>
        <LineChart points={returns} refLine={0} color={colors.accent} format={(v) => pct(v)} />
        <Pills options={RANGES.map((r) => r[0])} value={range} onChange={setRange} />
      </Card>

      <T size={18} weight="heavy" style={{ marginTop: 12 }}>Portfolios</T>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ marginHorizontal: -20 }}
        contentContainerStyle={{ paddingHorizontal: 20, gap: 12 }}>
        {shown.map((p, i) => <PortfolioCard key={p.id} p={p} index={i} />)}
      </ScrollView>

      <Link href="/automations" asChild>
        <Pressable accessibilityRole="button">
          <Card style={{ marginTop: 4, flexDirection: 'row', alignItems: 'center', gap: 12 }}>
            <View style={{ width: 40, height: 40, borderRadius: 12, backgroundColor: colors.accentSoft, alignItems: 'center',
              justifyContent: 'center' }}>
              <SymbolView name={{ ios: 'bolt.fill', android: 'bolt', web: 'bolt' }} tintColor={colors.accent} size={18} />
            </View>
            <View style={{ flex: 1 }}>
              <T weight="bold">{active} automations running</T>
              <T size={12} color={colors.muted}>{autos.length - active} paused</T>
            </View>
            <SymbolView name={{ ios: 'chevron.right', android: 'chevron_right', web: 'chevron_right' }} tintColor={colors.muted} size={16} />
          </Card>
        </Pressable>
      </Link>
    </Screen>
  );
}
