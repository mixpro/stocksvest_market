// The emergency stop (POST /v1/orders/kill-all): switch automations off, cancel open buys, sell every holding the
// app's portfolios hold, paper and live. Slide, then Face ID.

import { router } from 'expo-router';
import { useState } from 'react';
import { Pressable, View } from 'react-native';

import { SlideToConfirm } from '@/components/SlideToConfirm';
import { Card, ErrorNote, Screen, T } from '@/components/ui';
import { useApp } from '@/lib/app';
import { confirmWithFaceId, done } from '@/lib/confirm';
import { usePortfolios } from '@/lib/portfolios';

type Result = { cancelled: string[]; sells: unknown[]; errors: string[] };

export default function KillAll() {
  const { api, colors } = useApp();
  const { portfolios, error } = usePortfolios();
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const holdings = portfolios.reduce((s, p) => s + p.positions.length, 0);
  const withShares = portfolios.filter((p) => p.positions.length).length;
  const live = portfolios.some((p) => p.env === 'LIVE' && p.positions.length);

  async function killAll() {
    if (!(await confirmWithFaceId('Sell everything'))) return;
    setSending(true);
    try {
      const r = await api<Result>('/v1/orders/kill-all', { method: 'POST', body: JSON.stringify({ confirm: 'SELL ALL' }) });
      setResult(r);
      done(!r.errors.length);
    } catch (e) {
      done(false);
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  const step = (n: number, text: string, red?: boolean) => (
    <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12, height: 50 }}>
      <View style={{ width: 26, height: 26, borderRadius: 8, alignItems: 'center', justifyContent: 'center',
        backgroundColor: red ? colors.dangerSoft : colors.accentSoft }}>
        <T size={13} weight="heavy" color={red ? colors.danger : colors.accent}>{n}</T>
      </View>
      <T weight="semibold" style={{ flex: 1 }}>{text}</T>
    </View>
  );

  if (result) {
    return (
      <Screen back title="Kill all">
        <Card style={{ alignItems: 'center', gap: 6, paddingVertical: 28 }}>
          <T size={22} weight="heavy">{result.errors.length ? 'Done, with problems' : 'Sell orders placed'}</T>
          <T center color={colors.muted}>
            Automations are off. {result.sells.length} sells placed, {result.cancelled.length} buys cancelled.
          </T>
        </Card>
        {result.errors.map((e) => <T key={e} size={13} color={colors.down}>{e}</T>)}
        <Pressable onPress={() => router.replace('/activity')} accessibilityRole="button"
          style={{ height: 56, borderRadius: 18, backgroundColor: colors.text, alignItems: 'center', justifyContent: 'center' }}>
          <T weight="heavy" color={colors.bg}>Watch fills</T>
        </Pressable>
      </Screen>
    );
  }

  return (
    <Screen back title="Kill all">
      <ErrorNote error={error ?? failed} />
      <View style={{ alignItems: 'center', gap: 6, marginTop: 12 }}>
        <T size={24} weight="heavy">Sell everything?</T>
        <T center color={colors.muted}>Paper and live, every portfolio. This can't be undone.</T>
        {live ? <T weight="bold" color={colors.danger}>Includes real money.</T> : null}
      </View>
      <Card style={{ paddingVertical: 4, backgroundColor: colors.card }}>
        {step(1, 'Switch off all automations')}
        {step(2, 'Cancel open buy orders')}
        {step(3, `Sell ${holdings} holdings in ${withShares} portfolios at market`, true)}
      </Card>
      <View style={{ marginTop: 12 }}>
        <SlideToConfirm label={sending ? 'Sending…' : 'Slide to sell all'} onConfirm={killAll} disabled={sending || !!error} />
      </View>
      <Pressable onPress={() => router.back()} accessibilityRole="button" style={{ height: 48, alignItems: 'center', justifyContent: 'center' }}>
        <T weight="bold">Cancel</T>
      </Pressable>
    </Screen>
  );
}
