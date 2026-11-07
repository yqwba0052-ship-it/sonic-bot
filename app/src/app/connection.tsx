import { useState } from 'react';
import { Platform } from 'react-native';

import { Body, Button, Card, colors, Field, Screen } from '@/components/ui';
import { errorCode, useBot } from '@/lib/bot-context';
import { message } from '@/lib/text';

export default function ConnectionScreen() {
  const { ready } = useBot();
  return ready ? <ConnectionForm /> : <Screen title="الاتصال">{null}</Screen>;
}

function ConnectionForm() {
  const { connection, connect, online } = useBot();
  const [url, setUrl] = useState(
    connection?.url ?? (Platform.OS === 'web' ? globalThis.location.origin : '')
  );
  const [token, setToken] = useState(connection?.token ?? '');
  const [notice, setNotice] = useState<{ text: string; color: string } | null>(null);

  const save = async () => {
    try {
      await connect({ url: url.trim(), token: token.trim() });
      setNotice({ text: 'تم الاتصال بالخادم.', color: colors.gain });
    } catch (failure) {
      setNotice({ text: message(errorCode(failure)), color: colors.loss });
    }
  };

  return (
    <Screen title="الاتصال">
      <Card>
        <Body color={online ? colors.gain : colors.muted}>{online ? 'متصل' : 'غير متصل'}</Body>
        <Field
          label="عنوان الخادم"
          hint="يظهر في نافذة الخادم على اللابتوب. لازم يكون الجوال على نفس الواي فاي."
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          placeholder="http://192.168.1.10:8787"
          value={url}
          onChangeText={setUrl}
        />
        <Field
          label="رمز الدخول"
          hint="يظهر في نافذة الخادم، ويُحفظ مشفرًا على الجوال."
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry
          value={token}
          onChangeText={setToken}
        />
      </Card>
      {notice ? <Body color={notice.color}>{notice.text}</Body> : null}
      <Button label="اتصل" disabled={!url.trim() || !token.trim()} onPress={save} />
    </Screen>
  );
}
