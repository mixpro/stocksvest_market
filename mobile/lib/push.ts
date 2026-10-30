// Push notifications: ask once, then send this phone's Expo push token to the app service on every start.

import Constants from 'expo-constants';
import * as Notifications from 'expo-notifications';
import { useEffect } from 'react';
import { Platform } from 'react-native';

import { useApp } from '@/lib/app';

Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: false }),
});

/** This phone's Expo push token, or null when notifications are refused or unavailable (Expo Go, simulator). */
export async function pushToken(): Promise<string | null> {
  if (Platform.OS === 'android') {
    await Notifications.setNotificationChannelAsync('default', { name: 'Default', importance: Notifications.AndroidImportance.MAX });
  }
  let { granted } = await Notifications.getPermissionsAsync();
  if (!granted) granted = (await Notifications.requestPermissionsAsync()).granted;
  if (!granted) return null;
  const projectId = Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
  return (await Notifications.getExpoPushTokenAsync({ projectId })).data;
}

/** Register with the server once the API key is known (and again whenever it changes). */
export function usePushRegistration() {
  const { ready, apiKey, api } = useApp();
  useEffect(() => {
    if (!ready || !apiKey) return;
    pushToken()
      .then((token) => token && api('/v1/push/devices', { method: 'POST', body: JSON.stringify({ token }) }))
      .catch(() => {}); // ponytail: no token in Expo Go or offline; the next start tries again
  }, [ready, apiKey, api]);
}
