import { Tabs } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { type ColorValue, Platform, Text } from 'react-native';

import { colors } from '@/components/ui';
import { BotProvider } from '@/lib/bot-context';

if (Platform.OS === 'web') {
  document.documentElement.dir = 'rtl';
  document.documentElement.lang = 'ar';
}

function icon(glyph: string) {
  function TabIcon({ color }: { color: ColorValue }) {
    return <Text style={{ color, fontSize: 20 }}>{glyph}</Text>;
  }
  return TabIcon;
}

export default function Layout() {
  return (
    <BotProvider>
      <StatusBar style="light" />
      <Tabs
        screenOptions={{
          headerShown: false,
          tabBarActiveTintColor: colors.text,
          tabBarInactiveTintColor: colors.muted,
          tabBarStyle: {
            backgroundColor: colors.card,
            borderTopColor: colors.line,
            ...(Platform.OS === 'web' ? { height: 64, paddingBottom: 8 } : null),
          },
        }}>
        <Tabs.Screen name="index" options={{ title: 'الحالة', tabBarIcon: icon('◉') }} />
        <Tabs.Screen name="safety" options={{ title: 'الأمان', tabBarIcon: icon('⛨') }} />
        <Tabs.Screen name="trades" options={{ title: 'الصفقات', tabBarIcon: icon('☰') }} />
        <Tabs.Screen name="connection" options={{ title: 'الاتصال', tabBarIcon: icon('⇄') }} />
      </Tabs>
    </BotProvider>
  );
}
