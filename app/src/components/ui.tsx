import { type ReactNode, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  type TextInputProps,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

export const colors = {
  background: '#0B0F14',
  card: '#151B23',
  line: '#26303B',
  text: '#F2F5F8',
  muted: '#9AA7B4',
  gain: '#2FBF71',
  loss: '#F0524F',
  warn: '#F2B441',
  accent: '#3B82F6',
};

export function Screen({ title, children }: { title: string; children: ReactNode }) {
  return (
    <SafeAreaView style={styles.screen} edges={['top']}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text style={styles.title}>{title}</Text>
        {children}
      </ScrollView>
    </SafeAreaView>
  );
}

export function Card({ children, tone }: { children: ReactNode; tone?: string }) {
  return <View style={[styles.card, tone ? { borderColor: tone } : null]}>{children}</View>;
}

export function Row({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.muted}>{label}</Text>
      <Text style={[styles.value, color ? { color } : null]}>{value}</Text>
    </View>
  );
}

export function Body({ children, color }: { children: ReactNode; color?: string }) {
  return <Text style={[styles.body, color ? { color } : null]}>{children}</Text>;
}

type ButtonProps = {
  label: string;
  onPress: () => void | Promise<void>;
  color?: string;
  disabled?: boolean;
};

export function Button({ label, onPress, color = colors.accent, disabled }: ButtonProps) {
  const [busy, setBusy] = useState(false);
  const press = async () => {
    setBusy(true);
    try {
      await onPress();
    } finally {
      setBusy(false);
    }
  };
  const off = disabled || busy;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: off }}
      disabled={off}
      onPress={press}
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: color, opacity: off ? 0.45 : pressed ? 0.8 : 1 },
      ]}>
      {busy ? <ActivityIndicator color="#fff" /> : <Text style={styles.buttonText}>{label}</Text>}
    </Pressable>
  );
}

/** Needs a second press within four seconds, so one touch cannot trigger it. */
export function ConfirmButton({ label, confirmLabel, onPress, color, disabled }: ButtonProps & {
  confirmLabel: string;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(timer);
  }, [armed]);
  return (
    <Button
      label={armed ? confirmLabel : label}
      color={color}
      disabled={disabled}
      onPress={async () => {
        if (!armed) {
          setArmed(true);
          return;
        }
        setArmed(false);
        await onPress();
      }}
    />
  );
}

export function Field({ label, hint, ...input }: TextInputProps & { label: string; hint?: string }) {
  return (
    <View style={styles.field}>
      <Text style={styles.label}>{label}</Text>
      <TextInput
        placeholderTextColor={colors.muted}
        style={styles.input}
        accessibilityLabel={label}
        {...input}
      />
      {hint ? <Text style={styles.hint}>{hint}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  content: { padding: 16, gap: 12, paddingBottom: 32 },
  title: { color: colors.text, fontSize: 26, fontWeight: '700', marginBottom: 4 },
  card: {
    backgroundColor: colors.card,
    borderColor: colors.line,
    borderWidth: 1,
    borderRadius: 12,
    padding: 14,
    gap: 8,
  },
  row: { flexDirection: 'row', justifyContent: 'space-between', gap: 12 },
  muted: { color: colors.muted, fontSize: 15 },
  value: { color: colors.text, fontSize: 15, fontWeight: '600', fontVariant: ['tabular-nums'] },
  body: { color: colors.text, fontSize: 15, lineHeight: 23 },
  button: {
    minHeight: 50,
    borderRadius: 10,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 16,
  },
  buttonText: { color: '#fff', fontSize: 16, fontWeight: '700' },
  field: { gap: 6 },
  label: { color: colors.text, fontSize: 15, fontWeight: '600' },
  hint: { color: colors.muted, fontSize: 13, lineHeight: 19 },
  input: {
    backgroundColor: colors.background,
    borderColor: colors.line,
    borderWidth: 1,
    borderRadius: 10,
    color: colors.text,
    fontSize: 16,
    minHeight: 48,
    paddingHorizontal: 12,
  },
});
