// Small building blocks shared by the screens.

import { ReactNode, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleProp, Text, TextStyle, View, ViewStyle } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { router } from 'expo-router';

import { useApp } from '@/lib/app';
import { font } from '@/lib/theme';

export const TAB_BAR_SPACE = 110; // room under the content for the floating tab bar

export function Screen({ title, subtitle, back, children, onRefresh, refreshing = false, footer }: {
  title?: string; subtitle?: string; back?: boolean; children: ReactNode; onRefresh?: () => void; refreshing?: boolean;
  footer?: ReactNode;
}) {
  const { colors } = useApp();
  const insets = useSafeAreaInsets();
  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <ScrollView
        contentContainerStyle={{ paddingTop: insets.top + 12, paddingBottom: footer ? 120 : TAB_BAR_SPACE, paddingHorizontal: 20, gap: 14 }}
        refreshControl={onRefresh ? <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.muted} /> : undefined}>
        {back ? (
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
            <Pressable onPress={() => router.back()} accessibilityRole="button" accessibilityLabel="Back"
              style={{ width: 44, height: 44, borderRadius: 22, backgroundColor: colors.card, alignItems: 'center', justifyContent: 'center' }}>
              <T size={22} weight="bold">‹</T>
            </Pressable>
            <View style={{ flex: 1 }}>
              {title ? <T size={18} weight="heavy">{title}</T> : null}
              {subtitle ? <T size={11} weight="bold" color={colors.accent}>{subtitle}</T> : null}
            </View>
          </View>
        ) : title ? <T size={26} weight="heavy">{title}</T> : null}
        {children}
      </ScrollView>
      {footer ? (
        <View style={{ position: 'absolute', left: 20, right: 20, bottom: insets.bottom + 12, flexDirection: 'row', gap: 12 }}>{footer}</View>
      ) : null}
    </View>
  );
}

export function T({ children, size = 14, weight = 'regular', color, style, center }: {
  children: ReactNode; size?: number; weight?: keyof typeof font; color?: string;
  style?: StyleProp<TextStyle>; center?: boolean;
}) {
  const { colors } = useApp();
  return (
    <Text style={[{ fontFamily: font[weight], fontSize: size, color: color ?? colors.text, fontVariant: ['tabular-nums'] },
      center && { textAlign: 'center' }, style]}>
      {children}
    </Text>
  );
}

export function Card({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  const { colors } = useApp();
  return <View style={[{ backgroundColor: colors.card, borderRadius: 18, padding: 16 }, style]}>{children}</View>;
}

export function Segmented<V extends string>({ options, value, onChange }: {
  options: { value: V; label: string }[]; value: V; onChange: (v: V) => void;
}) {
  const { colors } = useApp();
  return (
    <View style={{ flexDirection: 'row', backgroundColor: colors.segment, borderRadius: 14, padding: 4 }}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <Pressable key={o.value} onPress={() => onChange(o.value)} accessibilityRole="button" accessibilityState={{ selected: on }}
            style={{ flex: 1, height: 38, borderRadius: 11, alignItems: 'center', justifyContent: 'center',
              backgroundColor: on ? colors.segmentOn : 'transparent' }}>
            <T weight="bold" size={13} color={on ? colors.text : colors.muted}>{o.label}</T>
          </Pressable>
        );
      })}
    </View>
  );
}

/** Small pill buttons (chart ranges, filters). */
export function Pills<V extends string>({ options, value, onChange }: {
  options: readonly V[]; value: V; onChange: (v: V) => void;
}) {
  const { colors } = useApp();
  return (
    <View style={{ flexDirection: 'row', justifyContent: 'space-between', gap: 6 }}>
      {options.map((o) => {
        const on = o === value;
        return (
          <Pressable key={o} onPress={() => onChange(o)} accessibilityRole="button" accessibilityState={{ selected: on }}
            style={{ minWidth: 44, height: 34, paddingHorizontal: 10, borderRadius: 10, alignItems: 'center', justifyContent: 'center',
              backgroundColor: on ? colors.text : 'transparent' }}>
            <T size={13} weight="bold" color={on ? colors.bg : colors.muted}>{o}</T>
          </Pressable>
        );
      })}
    </View>
  );
}

/** A card with a title row that folds its content away. */
export function Section({ title, children, startOpen = true }: { title: string; children: ReactNode; startOpen?: boolean }) {
  const { colors } = useApp();
  const [open, setOpen] = useState(startOpen);
  return (
    <Card style={{ paddingVertical: 4 }}>
      <Pressable onPress={() => setOpen(!open)} accessibilityRole="button" accessibilityState={{ expanded: open }}
        style={{ height: 46, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <T weight="bold">{title}</T>
        <T size={16} color={colors.muted}>{open ? '▴' : '▾'}</T>
      </Pressable>
      {open ? <View style={{ paddingBottom: 12, gap: 10 }}>{children}</View> : null}
    </Card>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  const { colors } = useApp();
  if (!error) return null;
  return (
    <Card style={{ backgroundColor: colors.downSoft }}>
      <T weight="bold" color={colors.down}>Can't reach the app service</T>
      <T size={13} color={colors.down}>{error}. Check Tailscale and the server and key in Settings.</T>
    </Card>
  );
}

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
export const money = (n: number | null | undefined) => (n == null ? '–' : usd.format(n).replace('-', '−'));
export const signedMoney = (n: number) => (n >= 0 ? '+' : '') + money(n);
export const pct = (n: number | null | undefined) =>
  n == null ? '–' : (n >= 0 ? '+' : '−') + Math.abs(n).toFixed(2) + '%';
