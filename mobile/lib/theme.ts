// Colours of the approved mock-up: https://claude.ai/artifact/CZiDwPeW6D4QnAzrnti7bX

export const light = {
  bg: '#F4F5FB',
  card: '#FFFFFF',
  text: '#1B1D3A',
  muted: '#5F6384',
  line: '#ECEDF5',
  segment: '#E8E9F5',
  segmentOn: '#FFFFFF',
  accent: '#4F46E5',
  accentSoft: '#E3E2FF',
  up: '#0B7A61',
  upSoft: '#DDF5EE',
  down: '#B42335',
  downSoft: '#FDE8EA',
  danger: '#C9283A',
  dangerSoft: '#FFE9EC',
  track: '#C9CBDD',
};

export type Palette = typeof light;

export const dark: Palette = {
  bg: '#0E0F1E',
  card: '#1A1C33',
  text: '#F1F2FA',
  muted: '#A3A7C7',
  line: '#2A2C47',
  segment: '#1A1C33',
  segmentOn: '#2E3157',
  accent: '#B7B2FF',
  accentSoft: '#2A2752',
  up: '#4FD8B4',
  upSoft: '#12332C',
  down: '#FF9AA4',
  downSoft: '#3A1820',
  danger: '#FF8A96',
  dangerSoft: '#3A1820',
  track: '#3A3D5C',
};

// Portfolio card gradients, in turn (white text on all of them).
export const cardColors: [string, string][] = [
  ['#6C63FF', '#4A3FE0'],
  ['#F0638C', '#C9356A'],
  ['#4C9BF5', '#2B6FD6'],
  ['#13B596', '#0B8A72'],
];

export const font = {
  regular: 'PlusJakartaSans_400Regular',
  semibold: 'PlusJakartaSans_600SemiBold',
  bold: 'PlusJakartaSans_700Bold',
  heavy: 'PlusJakartaSans_800ExtraBold',
};

/** A card gradient as a style background (the mock-up's 140° two-stop fill); keep backgroundColor as the fallback. */
export const gradient = ([from, to]: [string, string]) => `linear-gradient(140deg, ${from}, ${to})`;
