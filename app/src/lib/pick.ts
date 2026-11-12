import * as DocumentPicker from 'expo-document-picker';
import { File } from 'expo-file-system';
import * as ImagePicker from 'expo-image-picker';
import { Platform } from 'react-native';

/** The phone's time zone, in minutes east of UTC (Riyadh: 180). */
export function deviceOffsetMinutes(): number {
  return -new Date().getTimezoneOffset();
}

export function offsetLabel(minutes: number): string {
  const sign = minutes >= 0 ? '+' : '-';
  const abs = Math.abs(minutes);
  return `UTC${sign}${Math.floor(abs / 60)}${abs % 60 ? `:${String(abs % 60).padStart(2, '0')}` : ''}`;
}

/** A CSV exported from Binance (or any CSV): its text, or null when nothing was chosen. */
export async function pickCsvText(): Promise<{ name: string; text: string } | null> {
  const result = await DocumentPicker.getDocumentAsync({
    type: ['text/csv', 'text/comma-separated-values', 'text/plain', 'application/vnd.ms-excel', '*/*'],
    copyToCacheDirectory: true,
    multiple: false,
  });
  if (result.canceled || !result.assets?.length) return null;
  const asset = result.assets[0];
  const text = Platform.OS === 'web' ? await (await fetch(asset.uri)).text() : await new File(asset.uri).text();
  return { name: asset.name, text };
}

/** A screenshot of the trader's position history, as base64 for the server's OCR. */
export async function pickScreenshotBase64(): Promise<string | null> {
  const result = await ImagePicker.launchImageLibraryAsync({
    mediaTypes: ['images'],
    base64: true,
    quality: 0.9,
    allowsMultipleSelection: false,
  });
  if (result.canceled || !result.assets?.length) return null;
  const asset = result.assets[0];
  if (asset.base64) return asset.base64;
  if (Platform.OS === 'web') {
    const blob = await (await fetch(asset.uri)).blob();
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(blob);
    });
    return data.slice(data.indexOf(',') + 1);
  }
  return null;
}
