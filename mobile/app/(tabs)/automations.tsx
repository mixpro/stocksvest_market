import { useState } from 'react';
import { Alert, View } from 'react-native';

import { Switch } from '@/components/Switch';
import { Card, ErrorNote, Screen, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import { done, tap } from '@/lib/confirm';
import { cardColors } from '@/lib/theme';

type Schedule = { type: string; onDays?: string[]; atTimes?: string[]; everyNWeeks?: number; timezone: string };
type Automation = {
  id: string; name: string; status: string; schedules: Schedule[];
  actions: { type: string; side?: string }[]; budgets: { name: string; portfolioId: string }[];
};
type SwitchState = { automationsEnabled: boolean; reason: string | null };

const DAYS: Record<string, string> = { MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu', FRI: 'Fri', SAT: 'Sat', SUN: 'Sun' };

function when(a: Automation) {
  const s = a.schedules[0];
  if (!s) return 'Run by hand';
  const times = (s.atTimes ?? []).map((t) => t.slice(0, 5)).join(', ');
  if (s.type === 'WEEKLY') return `${(s.onDays ?? []).map((d) => DAYS[d] ?? d).join(' ')} ${times} ET`;
  return `${s.type.charAt(0) + s.type.slice(1).toLowerCase()} ${times}`.trim();
}

export default function Automations() {
  const { api, colors } = useApp();
  const list = useApi<{ automations: Automation[] }>('/v1/automations');
  const master = useApi<SwitchState>('/v1/automations/switch');
  const portfolios = useApi<{ portfolios: { id: string; name: string }[] }>('/v1/portfolios');
  const [busy, setBusy] = useState<string | null>(null);

  async function run(key: string, call: () => Promise<unknown>, reload: () => Promise<void>) {
    tap();
    setBusy(key);
    try {
      await call();
      done(true);
    } catch (e) {
      done(false);
      Alert.alert('Not changed', e instanceof Error ? e.message : String(e));
    } finally {
      await reload();
      setBusy(null);
    }
  }

  const setMaster = (on: boolean) =>
    run('master', () => api('/v1/automations/switch', { method: 'PUT',
      body: JSON.stringify({ automationsEnabled: on, reason: on ? null : 'switched off from the phone' }) }), master.reload);
  const setOne = (a: Automation, on: boolean) =>
    run(a.id, () => api(`/v1/automations/${a.id}/${on ? 'resume' : 'pause'}`, { method: 'POST' }), list.reload);

  const names = Object.fromEntries((portfolios.data?.portfolios ?? []).map((p) => [p.id, p.name]));
  const groups = new Map<string, Automation[]>();
  for (const a of list.data?.automations ?? []) {
    const g = names[a.budgets[0]?.portfolioId] ?? 'Not linked to a budget';
    groups.set(g, [...(groups.get(g) ?? []), a]);
  }
  const on = master.data?.automationsEnabled ?? true;

  return (
    <Screen title="Automations" onRefresh={() => { list.reload(); master.reload(); }} refreshing={list.loading}>
      <ErrorNote error={list.error ?? master.error} />
      <View style={{ padding: 18, borderRadius: 22, flexDirection: 'row', alignItems: 'center', gap: 14,
        backgroundColor: on ? cardColors[0][1] : '#5F6384' }}>
        <View style={{ flex: 1 }}>
          <T size={12} weight="semibold" color="#FFFFFF">All scheduled runs</T>
          <T size={20} weight="heavy" color="#FFFFFF">{on ? 'Running' : 'Switched off'}</T>
          <T size={12} color="#FFFFFF">{on ? 'Turn off to stop every automation at once' : master.data?.reason ?? 'Nothing runs until you turn this on'}</T>
        </View>
        <Switch on={on} onChange={setMaster} label="All scheduled runs" disabled={busy === 'master'} onLight />
      </View>

      {[...groups].map(([group, autos]) => (
        <View key={group} style={{ gap: 8 }}>
          <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
            <T weight="heavy">{group}</T>
            <T size={12} color={colors.muted}>{autos.filter((a) => a.status === 'ACTIVE').length} of {autos.length} on</T>
          </View>
          <Card style={{ paddingVertical: 0 }}>
            {autos.map((a, i) => {
              const sell = a.actions.some((x) => x.side === 'SELL');
              const active = a.status === 'ACTIVE';
              return (
                <View key={a.id} style={{ flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: 58, paddingVertical: 8,
                  borderTopWidth: i ? 1 : 0, borderTopColor: colors.line }}>
                  <View style={{ width: 44, height: 24, borderRadius: 7, alignItems: 'center', justifyContent: 'center',
                    backgroundColor: sell ? colors.downSoft : colors.accentSoft }}>
                    <T size={10} weight="heavy" color={sell ? colors.down : colors.accent}>{sell ? 'SELL' : 'BUY'}</T>
                  </View>
                  <View style={{ flex: 1 }}>
                    <T weight="bold">{a.name}</T>
                    <T size={12} color={colors.muted}>{active ? when(a) : a.status === 'PAUSED' ? 'Paused' : a.status}</T>
                  </View>
                  <Switch on={active} onChange={(v) => setOne(a, v)} label={a.name} disabled={busy === a.id} />
                </View>
              );
            })}
          </Card>
        </View>
      ))}
    </Screen>
  );
}
