// A smooth-enough line chart with a soft fill, drawn with react-native-svg. Optional dashed reference line (zero, or
// your average cost) and a press-and-drag readout.

import { useState } from 'react';
import { GestureResponderEvent, View } from 'react-native';
import Svg, { Defs, Line, LinearGradient, Path, Stop } from 'react-native-svg';

import { T } from '@/components/ui';
import { useApp } from '@/lib/app';

export type Point = { at: string; value: number };

export function LineChart({ points, color, height = 140, refLine, format }: {
  points: Point[]; color: string; height?: number; refLine?: number; format: (v: number) => string;
}) {
  const { colors } = useApp();
  const [width, setWidth] = useState(0);
  const [hover, setHover] = useState<number | null>(null);
  if (points.length < 2) {
    return <View style={{ height, justifyContent: 'center' }}><T center color={colors.muted} size={13}>Not enough data yet</T></View>;
  }
  const values = points.map((p) => p.value).concat(refLine ?? []);
  const lo = Math.min(...values), hi = Math.max(...values);
  const pad = (hi - lo || 1) * 0.12;
  const y = (v: number) => height - ((v - lo + pad) / (hi - lo + 2 * pad)) * height;
  const x = (i: number) => (i / (points.length - 1)) * width;
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.value).toFixed(1)}`).join(' ');

  const pick = (e: GestureResponderEvent) =>
    setHover(Math.max(0, Math.min(points.length - 1, Math.round((e.nativeEvent.locationX / width) * (points.length - 1)))));
  const shown = points[hover ?? points.length - 1];

  return (
    <View>
      <T size={12} weight="bold" color={colors.muted}>
        {new Date(shown.at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · {format(shown.value)}
      </T>
      <View style={{ height, marginTop: 6 }} onLayout={(e) => setWidth(e.nativeEvent.layout.width)}
        onStartShouldSetResponder={() => true} onResponderGrant={pick} onResponderMove={pick} onResponderRelease={() => setHover(null)}>
        {width > 0 ? (
          <Svg width={width} height={height}>
            <Defs>
              <LinearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
                <Stop offset="0" stopColor={color} stopOpacity={0.28} />
                <Stop offset="1" stopColor={color} stopOpacity={0} />
              </LinearGradient>
            </Defs>
            {refLine != null ? (
              <Line x1={0} x2={width} y1={y(refLine)} y2={y(refLine)} stroke={colors.muted} strokeDasharray="3 5" />
            ) : null}
            <Path d={`${line} L${width} ${height} L0 ${height} Z`} fill="url(#fill)" />
            <Path d={line} fill="none" stroke={color} strokeWidth={2.6} strokeLinejoin="round" strokeLinecap="round" />
            {hover != null ? (
              <Line x1={x(hover)} x2={x(hover)} y1={0} y2={height} stroke={color} strokeWidth={1.5} strokeDasharray="3 4" />
            ) : null}
          </Svg>
        ) : null}
      </View>
    </View>
  );
}

export const RANGES = [['1W', 7], ['1M', 30], ['3M', 91], ['1Y', 365], ['All', null]] as const;
export type Range = (typeof RANGES)[number][0];

export const sinceDays = (days: number | null) =>
  days == null ? null : new Date(Date.now() - days * 86400000).toISOString().slice(0, 19) + 'Z';
