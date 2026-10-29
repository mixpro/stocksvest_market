import { Tabs } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import type { ComponentProps } from 'react';

import { useApp } from '@/lib/app';
import { font } from '@/lib/theme';

type Name = ComponentProps<typeof SymbolView>['name'];

const TABS: { name: string; title: string; icon: Name }[] = [
  { name: 'index', title: 'Home', icon: { ios: 'house.fill', android: 'home', web: 'home' } },
  { name: 'portfolios', title: 'Portfolios', icon: { ios: 'chart.pie.fill', android: 'pie_chart', web: 'pie_chart' } },
  { name: 'automations', title: 'Automate', icon: { ios: 'bolt.fill', android: 'bolt', web: 'bolt' } },
  { name: 'activity', title: 'Activity', icon: { ios: 'waveform.path.ecg', android: 'monitoring', web: 'monitoring' } },
  { name: 'settings', title: 'Settings', icon: { ios: 'slider.horizontal.3', android: 'tune', web: 'tune' } },
];

export default function TabLayout() {
  const { colors, scheme } = useApp();
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.muted,
        tabBarLabelStyle: { fontFamily: font.bold, fontSize: 11 },
        // The floating rounded bar of the mock-up.
        tabBarStyle: {
          position: 'absolute', left: 16, right: 16, bottom: 22, marginHorizontal: 16, height: 70, paddingTop: 8,
          paddingBottom: 8, borderRadius: 26, borderTopWidth: 0, backgroundColor: colors.card,
          shadowColor: '#28285A', shadowOpacity: scheme === 'dark' ? 0.4 : 0.12, shadowRadius: 15,
          shadowOffset: { width: 0, height: 10 }, elevation: 8,
        },
      }}>
      {TABS.map((t) => (
        <Tabs.Screen key={t.name} name={t.name} options={{
          title: t.title,
          tabBarIcon: ({ color }) => <SymbolView name={t.icon} tintColor={color} size={22} />,
        }} />
      ))}
    </Tabs>
  );
}
