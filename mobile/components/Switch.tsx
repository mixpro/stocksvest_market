import { Pressable, View } from 'react-native';

import { useApp } from '@/lib/app';

/** The mock-up's pill switch. `onLight` draws it on a coloured card. */
export function Switch({ on, onChange, label, disabled, onLight }: {
  on: boolean; onChange: (on: boolean) => void; label: string; disabled?: boolean; onLight?: boolean;
}) {
  const { colors } = useApp();
  return (
    <Pressable onPress={() => onChange(!on)} disabled={disabled} accessibilityRole="switch" accessibilityLabel={label}
      accessibilityState={{ checked: on, disabled }} hitSlop={8}
      style={{ width: 52, height: 30, borderRadius: 15, padding: 3, opacity: disabled ? 0.5 : 1,
        backgroundColor: onLight ? 'rgba(255,255,255,0.3)' : on ? colors.accent : colors.track,
        alignItems: on ? 'flex-end' : 'flex-start' }}>
      <View style={{ width: 24, height: 24, borderRadius: 12, backgroundColor: '#FFFFFF' }} />
    </Pressable>
  );
}
