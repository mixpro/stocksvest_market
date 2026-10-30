import { useState } from 'react';
import { Pressable, TextInput, View } from 'react-native';

import { Switch } from '@/components/Switch';
import { Card, ErrorNote, Screen, Segmented, T } from '@/components/ui';
import { ThemePref, useApi, useApp } from '@/lib/app';
import { font } from '@/lib/theme';

type PushState = { devices: number; alertsEnabled: boolean; categories: { key: string; label: string; enabled: boolean }[] };

export default function Settings() {
  const { serverUrl, apiKey, themePref, save, colors, api } = useApp();
  const push = useApi<PushState>(apiKey ? '/v1/push' : null);
  const [pushNote, setPushNote] = useState<string | null>(null);

  async function toggle(key: string, on: boolean) {
    await api('/v1/push/categories', { method: 'PUT', body: JSON.stringify({ [key]: on }) }).catch((e) => setPushNote(String(e.message ?? e)));
    push.reload();
  }

  async function test() {
    const r = await api<{ sent: number }>('/v1/push/test', { method: 'POST' }).catch(() => ({ sent: 0 }));
    setPushNote(r.sent ? 'Sent. It should arrive in a few seconds.' : 'Not sent: no phone is registered yet.');
  }
  const [url, setUrl] = useState(serverUrl);
  const [key, setKey] = useState(apiKey);
  const [status, setStatus] = useState<{ ok: boolean; text: string } | null>(null);

  const input = {
    height: 48, borderRadius: 14, paddingHorizontal: 14, backgroundColor: colors.bg, color: colors.text,
    fontFamily: font.semibold, fontSize: 14,
  };

  async function saveAndTest() {
    await save({ serverUrl: url.trim(), apiKey: key.trim() });
    setStatus({ ok: true, text: 'Checking…' });
    try {
      // api() still has the old values this render, so test with a direct call.
      const res = await fetch(url.trim().replace(/\/$/, '') + '/v1/automations/switch', { headers: { 'X-API-Key': key.trim() } });
      setStatus(res.ok ? { ok: true, text: 'Connected' } : { ok: false, text: res.status === 401 ? 'Wrong API key' : `HTTP ${res.status}` });
    } catch (e) {
      setStatus({ ok: false, text: `Can't reach the server (${e instanceof Error ? e.message : e}). Is Tailscale on?` });
    }
  }

  return (
    <Screen title="Settings">
      <T size={12} weight="bold" color={colors.muted}>THEME</T>
      <Segmented<ThemePref> value={themePref} onChange={(v) => save({ themePref: v })}
        options={[{ value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }, { value: 'system', label: 'System' }]} />

      <T size={12} weight="bold" color={colors.muted} style={{ marginTop: 8 }}>SERVER</T>
      <Card style={{ gap: 10 }}>
        <T size={13} weight="bold">App service address</T>
        <TextInput value={url} onChangeText={setUrl} autoCapitalize="none" autoCorrect={false} keyboardType="url"
          style={input} accessibilityLabel="App service address" />
        <T size={13} weight="bold">API key</T>
        <TextInput value={key} onChangeText={setKey} autoCapitalize="none" autoCorrect={false} secureTextEntry
          placeholder="Paste the app API key" placeholderTextColor={colors.muted} style={input} accessibilityLabel="API key" />
        <Pressable onPress={saveAndTest} accessibilityRole="button"
          style={{ height: 50, borderRadius: 16, backgroundColor: colors.accent, alignItems: 'center', justifyContent: 'center' }}>
          <T weight="heavy" color={colors.card}>Save and test</T>
        </Pressable>
        {status ? <T size={13} weight="bold" color={status.ok ? colors.up : colors.down}>{status.text}</T> : null}
      </Card>

      <T size={12} weight="bold" color={colors.muted} style={{ marginTop: 8 }}>NOTIFICATIONS</T>
      <ErrorNote error={push.error} />
      {push.data ? (
        <Card style={{ paddingVertical: 4 }}>
          {push.data.categories.map((c) => (
            <View key={c.key} style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', height: 50 }}>
              <T weight="semibold">{c.label}</T>
              <Switch on={c.enabled} onChange={(on) => toggle(c.key, on)} label={c.label} />
            </View>
          ))}
          <Pressable onPress={test} accessibilityRole="button" style={{ height: 48, justifyContent: 'center' }}>
            <T weight="bold" color={colors.accent}>Send a test notification</T>
          </Pressable>
        </Card>
      ) : null}
      {push.data && !push.data.alertsEnabled ? <T size={12} color={colors.down}>Alerts are off on the server (ALERTS_ENABLED).</T> : null}
      {pushNote ? <T size={12} weight="bold">{pushNote}</T> : null}
      <View style={{ height: 4 }} />
      <T size={12} color={colors.muted}>The key is kept in the phone's secure storage (Keychain on iPhone).</T>
    </Screen>
  );
}
