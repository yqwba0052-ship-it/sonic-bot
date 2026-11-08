import { useState } from 'react';
import { Platform, Pressable, StyleSheet, Text, View } from 'react-native';

import { Body, Button, Card, colors, Field, Screen } from '@/components/ui';
import type { Place } from '@/lib/api';
import { errorCode, useBot } from '@/lib/bot-context';
import { message } from '@/lib/text';

const PLACES: { place: Place; label: string }[] = [
  { place: 'laptop', label: 'اللابتوب' },
  { place: 'cloud', label: 'الخادم السحابي' },
];

const HINTS: Record<Place, { url: string; placeholder: string; token: string }> = {
  laptop: {
    url: 'يظهر في نافذة الخادم على اللابتوب. لازم يكون الجوال على نفس الواي فاي.',
    placeholder: 'http://192.168.1.10:8787',
    token: 'يظهر في نافذة الخادم، ويُحفظ مشفرًا على الجوال.',
  },
  cloud: {
    url: 'يطبعه ملف التثبيت على الخادم السحابي، ويبدأ بـ https. يعمل من أي مكان.',
    placeholder: 'https://1-2-3-4.sslip.io',
    token: 'يطبعه ملف التثبيت على الخادم السحابي.',
  },
};

export default function ConnectionScreen() {
  const { ready } = useBot();
  return ready ? <ConnectionForm /> : <Screen title="الاتصال">{null}</Screen>;
}

function ConnectionForm() {
  const { profiles, connect, switchTo, online, status, call } = useBot();
  const [place, setPlace] = useState<Place>(profiles.active);
  const [url, setUrl] = useState(
    profiles[profiles.active]?.url ?? (Platform.OS === 'web' ? globalThis.location.origin : '')
  );
  const [token, setToken] = useState(profiles[profiles.active]?.token ?? '');
  const [notice, setNotice] = useState<{ text: string; color: string } | null>(null);

  const choose = (next: Place) => {
    setPlace(next);
    setUrl(profiles[next]?.url ?? '');
    setToken(profiles[next]?.token ?? '');
    setNotice(null);
  };

  const save = async () => {
    try {
      await connect({ url: url.trim(), token: token.trim() }, place);
      setNotice({ text: 'تم الاتصال، وصار هذا الخادم هو المستخدم.', color: colors.gain });
    } catch (failure) {
      setNotice({ text: message(errorCode(failure)), color: colors.loss });
    }
  };

  const toggleAwake = async () => {
    if (!status) return;
    try {
      await call('/settings', 'PUT', { keep_awake: !status.keep_awake });
    } catch (failure) {
      setNotice({ text: message(errorCode(failure)), color: colors.loss });
    }
  };

  return (
    <Screen title="الاتصال">
      <Card>
        <Body>أين يعمل البوت؟</Body>
        <View style={styles.row}>
          {PLACES.map((item) => {
            const selected = item.place === place;
            const active = item.place === profiles.active;
            return (
              <Pressable
                key={item.place}
                accessibilityRole="radio"
                accessibilityState={{ selected }}
                accessibilityLabel={item.label}
                onPress={() => choose(item.place)}
                style={[styles.choice, selected && styles.chosen]}>
                <Text style={styles.choiceText}>{item.label}</Text>
                <Text style={styles.choiceHint}>
                  {active ? (online ? 'مستخدم الآن: متصل' : 'مستخدم الآن') : profiles[item.place] ? 'محفوظ' : 'غير مضبوط'}
                </Text>
              </Pressable>
            );
          })}
        </View>
        {place !== profiles.active && profiles[place] ? (
          <Button label={`استخدم ${place === 'cloud' ? 'الخادم السحابي' : 'اللابتوب'}`} onPress={() => switchTo(place)} />
        ) : null}
      </Card>
      <Card>
        <Field
          label="عنوان الخادم"
          hint={HINTS[place].url}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          placeholder={HINTS[place].placeholder}
          value={url}
          onChangeText={setUrl}
        />
        <Field
          label="رمز الدخول"
          hint={HINTS[place].token}
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
          value={token}
          onChangeText={setToken}
        />
      </Card>
      {notice ? <Body color={notice.color}>{notice.text}</Body> : null}
      <Button label="اتصل" disabled={!url.trim() || !token.trim()} onPress={save} />
      {status?.keep_awake_supported ? (
        <Card>
          <Pressable
            accessibilityRole="switch"
            accessibilityState={{ checked: status.keep_awake }}
            accessibilityLabel="منع اللابتوب من النوم"
            onPress={toggleAwake}
            style={[styles.choice, status.keep_awake && styles.chosen]}>
            <Text style={styles.choiceText}>
              {status.keep_awake ? '✓ ' : ''}منع اللابتوب من النوم أثناء عمل البوت
            </Text>
            <Text style={styles.choiceHint}>
              لا يغير إعدادات Windows، وينتهي عند إغلاق نافذة الخادم. الشاشة تقدر تنطفئ عادي، لكن
              إغلاق غطاء اللابتوب قد ينيمه حسب إعداداتك.
            </Text>
          </Pressable>
        </Card>
      ) : null}
      {place === 'cloud' ? (
        <Card>
          <Body>تشغيل 24 ساعة على خادم سحابي (اختياري)</Body>
          <Body color={colors.muted}>
            1. أنشئ أنت خادم Ubuntu 24.04 صغيرًا، مثل DigitalOcean بحوالي 6$ شهريًا، في منطقة غير
            أمريكا مثل فرانكفورت أو سنغافورة.{'\n'}
            2. افتح لوحة الأوامر فيه، والصق الأمر الموجود في ملف README في GitHub (deploy).{'\n'}
            3. انسخ العنوان والرمز اللذين يظهران، وضعهما هنا.{'\n'}
            البوت على الخادم السحابي يبدأ متوقفًا، فاضغط «شغّل البوت» بعد الاتصال.
          </Body>
        </Card>
      ) : null}
    </Screen>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', gap: 8 },
  choice: {
    flex: 1,
    minHeight: 56,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: colors.line,
    backgroundColor: colors.background,
    paddingHorizontal: 12,
    paddingVertical: 8,
    justifyContent: 'center',
  },
  chosen: { borderColor: colors.gain, backgroundColor: '#12291D' },
  choiceText: { color: colors.text, fontSize: 15, fontWeight: '700' },
  choiceHint: { color: colors.muted, fontSize: 12, marginTop: 2, lineHeight: 18 },
});
