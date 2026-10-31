// Watcher events (GET /v1/events): what each watcher found and what the automations did about it. A LIVE run waits
// here for a confirm (Face ID) until it expires. Below: each watcher's switch and thresholds (PUT /v1/watchers/{name}).

import { useState } from 'react';
import { Pressable, TextInput, View } from 'react-native';

import { Switch } from '@/components/Switch';
import { Card, ErrorNote, Screen, Section, T } from '@/components/ui';
import { useApi, useApp } from '@/lib/app';
import { confirmWithFaceId, done } from '@/lib/confirm';
import { font } from '@/lib/theme';

type Outcome = { automationId: string; name: string; status: string; expiresAt?: string; skipReason?: string; error?: string };
type Event = {
  id: string; watcher: string; kind: string; symbol: string | null; severity: number; title: string; body: string;
  status: string; statusReason: string | null; outcomes: Outcome[]; createdAt: string;
};
type Watcher = { name: string; description: string; enabled: boolean; settings: Record<string, number>; lastError: string | null };

// dropPercent → Drop percent
const label = (key: string) => key.replace(/([A-Z])/g, ' $1').replace(/^./, (c) => c.toUpperCase()).replace(' percent', ' %');
const when = (iso: string) =>
  new Date(iso).toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' });

export default function Events() {
  const { api, colors } = useApp();
  const events = useApi<{ events: Event[] }>('/v1/events?limit=50');
  const watchers = useApi<{ watchers: Watcher[] }>('/v1/watchers');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function answer(ev: Event, yes: boolean) {
    if (yes && !(await confirmWithFaceId(`Run ${ev.outcomes.filter((o) => o.status === 'AWAITING_CONFIRM').map((o) => o.name).join(', ')}`))) return;
    setBusy(ev.id);
    try {
      await api(`/v1/events/${ev.id}/confirm`, { method: 'POST', body: JSON.stringify({ confirm: yes }) });
      done(true);
      setError(null);
    } catch (e) {
      done(false);
      setError(e instanceof Error ? e.message : String(e)); // 409: already answered or expired
    } finally {
      setBusy(null);
      events.reload();
    }
  }

  async function saveWatcher(name: string, change: { enabled?: boolean; settings?: Record<string, number> }) {
    await api(`/v1/watchers/${name}`, { method: 'PUT', body: JSON.stringify(change) })
      .then(() => setError(null), (e) => setError(e instanceof Error ? e.message : String(e)));
    watchers.reload();
  }

  const list = events.data?.events ?? [];
  const waiting = list.filter((e) => e.status === 'AWAITING_CONFIRM');
  const tone = (s: string) =>
    s === 'AWAITING_CONFIRM' ? [colors.dangerSoft, colors.danger] : s === 'FAILED' ? [colors.downSoft, colors.down]
      : s === 'DONE' ? [colors.upSoft, colors.up] : [colors.line, colors.muted];
  const input = {
    width: 70, height: 40, borderRadius: 12, paddingHorizontal: 10, backgroundColor: colors.bg, color: colors.text,
    fontFamily: font.semibold, fontSize: 14, textAlign: 'right' as const,
  };

  return (
    <Screen back title="Events" onRefresh={() => { events.reload(); watchers.reload(); }} refreshing={events.loading}>
      <ErrorNote error={events.error ?? error} />

      {waiting.map((ev) => (
        <Card key={ev.id} style={{ gap: 8, borderWidth: 2, borderColor: colors.danger }}>
          <T size={12} weight="heavy" color={colors.danger}>LIVE · WAITING FOR YOU</T>
          <T size={17} weight="heavy">{ev.title}</T>
          <T size={13} color={colors.muted}>{ev.body}</T>
          {ev.outcomes.filter((o) => o.status === 'AWAITING_CONFIRM').map((o) => (
            <T key={o.automationId} weight="bold">
              Run “{o.name}”? Until {new Date(o.expiresAt!).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
            </T>
          ))}
          <View style={{ flexDirection: 'row', gap: 10, marginTop: 4 }}>
            <Pressable onPress={() => answer(ev, false)} disabled={busy === ev.id} accessibilityRole="button"
              style={{ flex: 1, height: 50, borderRadius: 16, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center' }}>
              <T weight="heavy">Decline</T>
            </Pressable>
            <Pressable onPress={() => answer(ev, true)} disabled={busy === ev.id} accessibilityRole="button"
              style={{ flex: 1, height: 50, borderRadius: 16, backgroundColor: colors.danger, alignItems: 'center', justifyContent: 'center' }}>
              <T weight="heavy" color={colors.card}>{busy === ev.id ? 'Sending…' : 'Confirm'}</T>
            </Pressable>
          </View>
        </Card>
      ))}

      <Card style={{ paddingVertical: 0 }}>
        {list.length ? null : <T color={colors.muted} style={{ paddingVertical: 16 }}>Nothing found yet.</T>}
        {list.filter((e) => e.status !== 'AWAITING_CONFIRM').map((ev, n) => {
          const [bg, fg] = tone(ev.status);
          return (
            <View key={ev.id} style={{ paddingVertical: 12, gap: 3, borderTopWidth: n ? 1 : 0, borderTopColor: colors.line }}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <T weight="bold" style={{ flex: 1 }}>{ev.title}</T>
                <View style={{ paddingHorizontal: 8, paddingVertical: 3, borderRadius: 8, backgroundColor: bg }}>
                  <T size={10} weight="heavy" color={fg}>{ev.status}</T>
                </View>
              </View>
              <T size={12} color={colors.muted}>{when(ev.createdAt)} · {ev.watcher} · severity {ev.severity}</T>
              {ev.outcomes.map((o) => (
                <T key={o.automationId} size={12}>
                  {o.name}: {o.status.toLowerCase().replace('_', ' ')}{o.skipReason || o.error ? ` (${o.skipReason ?? o.error})` : ''}
                </T>
              ))}
              {!ev.outcomes.length && ev.statusReason ? <T size={12} color={colors.muted}>{ev.statusReason}</T> : null}
            </View>
          );
        })}
      </Card>

      <Section title="Watchers" startOpen={false}>
        <ErrorNote error={watchers.error} />
        {(watchers.data?.watchers ?? []).map((w) => (
          <View key={w.name} style={{ gap: 6, paddingTop: 10, borderTopWidth: 1, borderTopColor: colors.line }}>
            <View style={{ flexDirection: 'row', alignItems: 'center', gap: 10 }}>
              <View style={{ flex: 1 }}>
                <T weight="bold">{w.name}</T>
                <T size={12} color={colors.muted}>{w.description}</T>
              </View>
              <Switch on={w.enabled} onChange={(on) => saveWatcher(w.name, { enabled: on })} label={w.name} />
            </View>
            {Object.entries(w.settings).map(([k, v]) => (
              <View key={k} style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
                <T size={13} weight="semibold">{label(k)}</T>
                <TextInput defaultValue={String(v)} keyboardType="decimal-pad" style={input} accessibilityLabel={label(k)}
                  onEndEditing={(e) => {
                    const n = Number(e.nativeEvent.text.replace(',', '.'));
                    if (n > 0 && n !== v) saveWatcher(w.name, { settings: { [k]: n } });
                  }} />
              </View>
            ))}
            {w.lastError ? <T size={12} color={colors.down}>Last error: {w.lastError}</T> : null}
          </View>
        ))}
      </Section>
    </Screen>
  );
}
