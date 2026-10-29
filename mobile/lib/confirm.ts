import * as Haptics from 'expo-haptics';
import * as LocalAuthentication from 'expo-local-authentication';

/** Ask for Face ID (or the phone passcode). A phone without either set up is let through: it has no lock to ask. */
export async function confirmWithFaceId(reason: string): Promise<boolean> {
  if (!(await LocalAuthentication.hasHardwareAsync()) || !(await LocalAuthentication.isEnrolledAsync())) return true;
  const result = await LocalAuthentication.authenticateAsync({ promptMessage: reason });
  return result.success;
}

export const tap = () => Haptics.selectionAsync();
export const done = (ok: boolean) =>
  Haptics.notificationAsync(ok ? Haptics.NotificationFeedbackType.Success : Haptics.NotificationFeedbackType.Error);
