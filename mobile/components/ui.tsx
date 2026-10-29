// Small building blocks shared by the screens.

import { ReactNode } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleProp, Text, TextStyle, View, ViewStyle } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useApp } from '@/lib/app';
import { font } from '@/lib/theme';

export const TAB_BAR_SPACE = 110; // room under the content for the floating tab bar

export function Screen({ title, children, onRefresh, refreshing = false }: {
  title?: string; children: ReactNode; onRefresh?: () => void; refreshing?: boolean;
}) {
  const { colors } = useApp();
  const insets = useSafeAreaInsets();
  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: colors.bg }}
      contentContainerStyle={{ paddingTop: insets.top + 12, paddingBottom: TAB_BAR_SPACE, paddingHorizontal: 20, gap: 14 }}
      refreshControl={onRefresh ? <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.muted} /> : undefined}>
      {title ? <T size={26} weight="heavy">{title}</T> : null}
      {children}
    </ScrollView>
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
