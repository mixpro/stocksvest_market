// Reactions: event automations made on the phone. "When <watcher> finds <kind> at severity N on a stock I hold, sell
// all / half / X% from <budget>, or notify me." Saved as ordinary automations (POST /v1/automations with an
// eventTrigger), so the browser editor shows them too. Phone reactions only sell or notify; buys are browser-only.
// A sell is a limit order valid in extended hours, so a pre-market drop can be sold before the open.

import { useState } from 'react';
import { Pressable, TextInput, View } from 'react-native';

import { Switch } from '@/components/Switch';
import { Card, ErrorNote, Screen, Section, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import { confirmWithFaceId, done } from '@/lib/confirm';
import { usePortfolios } from '@/lib/portfolios';
import { font } from '@/lib/theme';

type Trigger = { watchers: string[]; kinds: string[]; minSeverity: number; cooldownMinutes: number; confirmWithinMinutes: number };
type Automation = {
  id: string; name: string; status: string; eventTrigger: Trigger | null; budgets?: { id: string; name: string }[];
  actions: { type: string; side?: string; sizings?: { mode: string; value: number }[] }[];
};
type Watcher = { name: string; kinds: string[] };
type Act = 'ALL' | 'HALF' | 'PCT' | 'NOTIFY';

const SELL_BELOW_PERCENT = -3; // the limit: up to 3% under the last trade, so it fills in a falling pre-market
const kindLabel = (k: string) => k.replace(/^NEWS_/, 'news: ').replaceAll('_', ' ').toLowerCase();

function Chips<V extends string | number>({ options, value, onChange }: {
  options: { value: V; label: string }[]; value: V; onChange: (v: V) => void;
}) {
  const { colors } = useApp();
  return (
    <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6 }}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <Pressable key={String(o.value)} onPress={() => onChange(o.value)} accessibilityRole="button"
            accessibilityState={{ selected: on }}
            style={{ minHeight: 36, paddingHorizontal: 12, borderRadius: 10, justifyContent: 'center',
              backgroundColor: on ? colors.text : colors.bg }}>
            <T size={13} weight="bold" color={on ? colors.bg : colors.muted}>{o.label}</T>
          </Pressable>
        );
      })}
    </View>
  );
}

/** "Sell half · holdings-drop · severity 4+" */
function describe(a: Automation): string {
  const t = a.eventTrigger!;
  const order = a.actions.find((x) => x.type === 'PLACE_ORDER');
  const pct = order?.sizings?.[0]?.value;
  const what = order ? (pct === 100 ? 'Sell all' : pct === 50 ? 'Sell half' : `Sell ${pct}%`) : 'Notify';
  const on = [t.watchers.join(', ') || 'any watcher', t.kinds.map(kindLabel).join(', ')].filter(Boolean).join(' · ');
  return `${what} · ${on} · severity ${t.minSeverity}+${a.budgets?.length ? ` · ${a.budgets.map((b) => b.name).join(', ')}` : ''}`;
}

export default function Reactions() {
  const { api, colors } = useApp();
  const autos = useApi<{ automations: Automation[] }>('/v1/automations');
  const watchers = useApi<{ watchers: Watcher[] }>('/v1/watchers');
  const { portfolios } = usePortfolios();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [watcher, setWatcher] = useState('');
  const [kind, setKind] = useState('');
  const [severity, setSeverity] = useState(4);
  const [act, setAct] = useState<Act>('ALL');
  const [percent, setPercent] = useState('25');
  const [budget, setBudget] = useState('');
  const [confirmMin, setConfirmMin] = useState('15');
  const [cooldownMin, setCooldownMin] = useState('60');

  const list = (autos.data?.automations ?? []).filter((a) => a.eventTrigger);
  const ws = watchers.data?.watchers ?? [];
  const kinds = (watcher ? ws.filter((w) => w.name === watcher) : ws).flatMap((w) => w.kinds);
  const budgets = portfolios.flatMap((p) => p.budgets.map((b) => ({ ...b, live: p.env === 'LIVE', label: `${p.name} · ${b.name}` })));
  const chosen = budgets.find((b) => b.id === budget);
  const sellPct = act === 'ALL' ? 100 : act === 'HALF' ? 50 : Number(percent.replace(',', '.'));
  const input = {
    width: 70, height: 40, borderRadius: 12, paddingHorizontal: 10, backgroundColor: colors.bg, color: colors.text,
    fontFamily: font.semibold, fontSize: 14, textAlign: 'right' as const,
  };

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      done(true);
      setError(null);
    } catch (e) {
      done(false);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      autos.reload();
    }
  }

  function save() {
    const sell = act !== 'NOTIFY';
    if (sell && !chosen) return setError('Pick the budget whose shares it sells.');
    if (sell && !(sellPct > 0 && sellPct <= 100)) return setError('The percentage must be between 1 and 100.');
    const what = sell ? (act === 'ALL' ? 'Sell all' : act === 'HALF' ? 'Sell half' : `Sell ${sellPct}%`) : 'Notify';
    const name = `${what} on ${kind ? kindLabel(kind) : watcher || 'any event'} ${severity}+`;
    run(async () => {
      if (sell && chosen!.live && !(await confirmWithFaceId(`Create "${name}" on real money`))) return;
      const a = await api<{ id: string }>('/v1/automations', { method: 'POST', body: JSON.stringify({
        name,
        eventTrigger: { watchers: watcher ? [watcher] : [], kinds: kind ? [kind] : [], minSeverity: severity,
          cooldownMinutes: Number(cooldownMin) || 0, confirmWithinMinutes: Number(confirmMin) || 15 },
        actions: [sell
          ? { type: 'PLACE_ORDER', side: 'SELL', orderType: 'LIMIT', timeInForce: 'DAY', extendedHours: true,
            limitOffsetPercent: SELL_BELOW_PERCENT, sizings: [{ mode: 'PERCENT_OF_POSITION', value: sellPct }] }
          : { type: 'NOTIFY', title: '{name}', message: '{event}' }],
        ...(sell ? { orderGuards: { regularHoursOnly: false } } : {}),
      }) });
      if (sell) await api(`/v1/budgets/${chosen!.id}/automations`, { method: 'POST', body: JSON.stringify({ automationId: a.id }) });
    });
  }

  return (
    <Screen back title="Reactions" subtitle="What happens when a watcher finds something" refreshing={autos.loading}
      onRefresh={() => { autos.reload(); watchers.reload(); }}>
      <ErrorNote error={autos.error ?? watchers.error ?? error} />

      <Card style={{ paddingVertical: 0 }}>
        {list.length ? null : <T color={colors.muted} style={{ paddingVertical: 16 }}>No reactions yet: watchers only notify.</T>}
        {list.map((a, n) => (
          <View key={a.id} style={{ paddingVertical: 12, gap: 6, borderTopWidth: n ? 1 : 0, borderTopColor: colors.line }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
              <T weight="bold" style={{ flex: 1 }}>{a.name}</T>
              <Switch on={a.status === 'ACTIVE'} label={a.name} disabled={busy}
                onChange={(on) => run(() => api(`/v1/automations/${a.id}/${on ? 'resume' : 'pause'}`, { method: 'POST' }))} />
            </View>
            <T size={12} color={colors.muted}>{describe(a)}</T>
            <Pressable accessibilityRole="button" disabled={busy}
              onPress={() => run(() => api(`/v1/automations/${a.id}`, { method: 'DELETE' }))}>
              <T size={12} weight="bold" color={colors.down}>Delete</T>
            </Pressable>
          </View>
        ))}
      </Card>

      <Section title="New reaction" startOpen={!list.length}>
        <T size={13} weight="semibold">When</T>
        <Chips options={[{ value: '', label: 'any watcher' }, ...ws.map((w) => ({ value: w.name, label: w.name }))]}
          value={watcher} onChange={(v) => { setWatcher(v); setKind(''); }} />
        <T size={13} weight="semibold">finds</T>
        <Chips options={[{ value: '', label: 'any kind' }, ...kinds.map((k) => ({ value: k, label: kindLabel(k) }))]}
          value={kind} onChange={setKind} />
        <T size={13} weight="semibold">at severity or more</T>
        <Chips options={[1, 2, 3, 4, 5].map((n) => ({ value: n, label: String(n) }))} value={severity} onChange={setSeverity} />
        <T size={13} weight="semibold">on a stock I hold</T>
        <Chips<Act> options={[{ value: 'ALL', label: 'Sell all' }, { value: 'HALF', label: 'Sell half' },
          { value: 'PCT', label: 'Sell %' }, { value: 'NOTIFY', label: 'Notify me' }]} value={act} onChange={setAct} />
        {act === 'PCT' ? (
          <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
            <T size={13} weight="semibold">Percent of the position</T>
            <TextInput value={percent} onChangeText={setPercent} keyboardType="decimal-pad" style={input} accessibilityLabel="Percent" />
          </View>
        ) : null}
        {act !== 'NOTIFY' ? (
          <>
            <T size={13} weight="semibold">from</T>
            <Chips options={budgets.map((b) => ({ value: b.id, label: `${b.label}${b.live ? ' · LIVE' : ''}` }))}
              value={budget} onChange={setBudget} />
            <T size={12} color={colors.muted}>
              {chosen?.live ? 'Real money: each sale waits for your Face ID confirm. ' : 'Paper: it sells at once. '}
              A limit order up to {-SELL_BELOW_PERCENT}% under the last trade, also before the open and after the close.
            </T>
            <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
              <T size={13} weight="semibold">Confirm within (min)</T>
              <TextInput value={confirmMin} onChangeText={setConfirmMin} keyboardType="number-pad" style={input}
                accessibilityLabel="Confirm within minutes" />
            </View>
          </>
        ) : (
          <T size={12} color={colors.muted}>A push of its own (category “Automation notifications”), even with watcher event pushes off.</T>
        )}
        <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
          <T size={13} weight="semibold">At most once every (min)</T>
          <TextInput value={cooldownMin} onChangeText={setCooldownMin} keyboardType="number-pad" style={input}
            accessibilityLabel="Cooldown minutes" />
        </View>
        <Pressable onPress={save} disabled={busy} accessibilityRole="button"
          style={{ height: 50, borderRadius: 16, backgroundColor: colors.accent, alignItems: 'center', justifyContent: 'center' }}>
          <T weight="heavy" color="#FFFFFF">{busy ? 'Saving…' : 'Save reaction'}</T>
        </Pressable>
      </Section>
    </Screen>
  );
}
