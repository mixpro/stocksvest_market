// Buy or sell at market (DAY). Fractions are fine at market. A live portfolio asks for Face ID first.

import { router, useLocalSearchParams } from 'expo-router';
import { useState } from 'react';
import { Pressable, TextInput, View } from 'react-native';

import { Card, ErrorNote, money, Screen, Segmented, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import { confirmWithFaceId, done } from '@/lib/confirm';
import type { Portfolio } from '@/lib/portfolios';
import { font } from '@/lib/theme';

type Order = { id: string; status: string; rejectReason: string | null; filledQuantity: number; averageFillPrice: number | null };

export default function OrderScreen() {
  const { portfolio, symbol, side } = useLocalSearchParams<{ portfolio: string; symbol: string; side: 'BUY' | 'SELL' }>();
  const { api, colors } = useApp();
  const p = useApi<Portfolio>(`/v1/portfolios/${portfolio}`);
  const accounts = useApi<{ accounts: { id: string; environment: string }[] }>('/v1/broker-accounts');
  const quote = useApi<{ quotes: Record<string, { price: number | null }> }>(`/v1/market/quotes?symbols=${symbol}`);
  const [picked, setOwner] = useState('manual');
  const [qty, setQty] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<Order | null>(null);

  const sell = side === 'SELL';
  const live = accounts.data?.accounts.find((a) => a.id === p.data?.brokerAccountId)?.environment === 'LIVE';
  const price = quote.data?.quotes[symbol]?.price ?? null;
  const held = (id: string) => p.data?.positions
    .filter((x) => x.symbol === symbol && (x.budgetId ?? 'manual') === id).reduce((s, x) => s + x.quantity, 0) ?? 0;
  // A sell picks whose shares go; a buy picks whose money pays (manual = the portfolio's free cash).
  const owners = [{ value: 'manual', label: 'Manual' }, ...(p.data?.budgets ?? []).map((b) => ({ value: b.id, label: b.name }))]
    .filter((o) => !sell || held(o.value) > 0);
  const owner = owners.some((o) => o.value === picked) ? picked : owners[0]?.value ?? 'manual';
  const quantity = Number(qty.replace(',', '.'));
  const valid = quantity > 0 && (!sell || quantity <= held(owner) + 1e-9);

  async function submit() {
    setError(null);
    if (live && !(await confirmWithFaceId(`${sell ? 'Sell' : 'Buy'} ${quantity} ${symbol} with real money`))) return;
    setSending(true);
    try {
      const o = await api<Order>(`/v1/portfolios/${portfolio}/orders`, { method: 'POST', body: JSON.stringify({
        symbol, side, type: 'MARKET', timeInForce: 'DAY', quantity, ...(owner === 'manual' ? {} : { budgetId: owner }) }) });
      setPlaced(o);
      done(o.status !== 'REJECTED');
    } catch (e) {
      done(false);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  const input = { height: 56, borderRadius: 16, paddingHorizontal: 16, backgroundColor: colors.card, color: colors.text,
    fontFamily: font.heavy, fontSize: 22 };

  if (placed) {
    return (
      <Screen back title={`${sell ? 'Sell' : 'Buy'} ${symbol}`}>
        <Card style={{ gap: 6, alignItems: 'center', paddingVertical: 28 }}>
          <T size={22} weight="heavy">{placed.status === 'REJECTED' ? 'Rejected' : 'Order placed'}</T>
          <T color={colors.muted} center>
            {placed.status === 'REJECTED' ? placed.rejectReason ?? '' : `Status: ${placed.status.toLowerCase()}. The fill shows in Activity.`}
          </T>
        </Card>
        <Pressable onPress={() => router.back()} accessibilityRole="button"
          style={{ height: 56, borderRadius: 18, backgroundColor: colors.text, alignItems: 'center', justifyContent: 'center' }}>
          <T weight="heavy" color={colors.bg}>Done</T>
        </Pressable>
      </Screen>
    );
  }

  return (
    <Screen back title={`${sell ? 'Sell' : 'Buy'} ${symbol}`} subtitle={p.data ? `${p.data.name}${live ? ' · REAL MONEY' : ''}` : undefined}
      footer={
        <Pressable onPress={submit} disabled={!valid || sending} accessibilityRole="button"
          style={{ flex: 1, height: 58, borderRadius: 18, alignItems: 'center', justifyContent: 'center', opacity: valid && !sending ? 1 : 0.4,
            backgroundColor: sell ? colors.danger : colors.accent }}>
          <T size={16} weight="heavy" color={colors.card}>
            {sending ? 'Sending…' : `${sell ? 'Sell' : 'Buy'} at market${live ? ' · Face ID' : ''}`}
          </T>
        </Pressable>
      }>
      <ErrorNote error={p.error} />
      <T size={13} color={colors.muted}>Price now {money(price)}</T>

      {owners.length > 1 ? (
        <>
          <T size={12} weight="bold" color={colors.muted}>{sell ? 'WHOSE SHARES' : 'PAID FROM'}</T>
          <Segmented value={owner} onChange={setOwner} options={owners} />
        </>
      ) : null}

      <T size={12} weight="bold" color={colors.muted}>SHARES</T>
      <TextInput value={qty} onChangeText={setQty} keyboardType="decimal-pad" placeholder="0" placeholderTextColor={colors.muted}
        style={input} accessibilityLabel="Shares" autoFocus />
      {sell ? (
        <Pressable onPress={() => setQty(String(held(owner)))} accessibilityRole="button">
          <T size={13} weight="bold" color={colors.accent}>All {+held(owner).toFixed(4)} shares</T>
        </Pressable>
      ) : null}

      <Card style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
        <T color={colors.muted}>{sell ? 'About' : 'Costs about'}</T>
        <T weight="heavy">{quantity > 0 && price ? money(quantity * price) : '–'}</T>
      </Card>
      {error ? <T weight="bold" color={colors.down}>{error}</T> : null}
      <View style={{ height: 8 }} />
      <T size={12} color={colors.muted}>Market order for today. Limit and stop orders stay on the web app for now.</T>
    </Screen>
  );
}
