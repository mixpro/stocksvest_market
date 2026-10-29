import { useRef, useState } from 'react';
import { Animated, PanResponder, View } from 'react-native';

import { T } from '@/components/ui';
import { useApp } from '@/lib/app';
import { tap } from '@/lib/confirm';

const KNOB = 52;

/** Drag the knob to the end to confirm; let go early and it springs back. */
export function SlideToConfirm({ label, onConfirm, disabled }: { label: string; onConfirm: () => void; disabled?: boolean }) {
  const { colors } = useApp();
  const x = useRef(new Animated.Value(0)).current;
  const [width, setWidth] = useState(0);
  const max = Math.max(width - KNOB - 10, 1);
  const pan = PanResponder.create({
    onStartShouldSetPanResponder: () => !disabled,
    onPanResponderGrant: () => tap(),
    onPanResponderMove: (_, g) => x.setValue(Math.max(0, Math.min(max, g.dx))),
    onPanResponderRelease: (_, g) => {
      if (g.dx >= max * 0.9) {
        Animated.timing(x, { toValue: max, duration: 80, useNativeDriver: true }).start(() => {
          onConfirm();
          x.setValue(0);
        });
      } else {
        Animated.spring(x, { toValue: 0, useNativeDriver: true }).start();
      }
    },
  });
  return (
    <View onLayout={(e) => setWidth(e.nativeEvent.layout.width)} accessibilityRole="adjustable" accessibilityLabel={label}
      accessibilityActions={[{ name: 'activate' }]} onAccessibilityAction={() => !disabled && onConfirm()}
      style={{ height: 62, borderRadius: 31, padding: 5, backgroundColor: colors.dangerSoft, justifyContent: 'center',
        opacity: disabled ? 0.5 : 1 }}>
      <T weight="heavy" color={colors.danger} center style={{ position: 'absolute', left: 0, right: 0 }}>{label}</T>
      <Animated.View {...pan.panHandlers}
        style={{ width: KNOB, height: KNOB, borderRadius: KNOB / 2, backgroundColor: '#D92D3F', alignItems: 'center',
          justifyContent: 'center', transform: [{ translateX: x }] }}>
        <T size={24} weight="heavy" color="#FFFFFF">›</T>
      </Animated.View>
    </View>
  );
}
