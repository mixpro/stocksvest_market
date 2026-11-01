// Watcher events shown as alerts. The phone remembers when you last looked at Activity; newer events (and LIVE runs
// waiting for a confirm) light the Home bell.

import * as SecureStore from 'expo-secure-store';

const KEY = 'alertsSeenAt';

export type WatcherEvent = {
  id: string; watcher: string; kind: string; symbol: string | null; severity: number; title: string; body: string;
  status: string; statusReason: string | null; createdAt: string;
};

export const seenAt = async () => new Date((await SecureStore.getItemAsync(KEY)) ?? 0);
export const markSeen = () => SecureStore.setItemAsync(KEY, new Date().toISOString());

/** Events you haven't seen yet, plus every LIVE run still waiting for you. */
export const unseen = (events: WatcherEvent[], since: Date) =>
  events.filter((e) => e.status === 'AWAITING_CONFIRM' || new Date(e.createdAt) > since).length;
