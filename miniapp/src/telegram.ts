import { TelegramStore, type TelegramStorageApi } from "./storage";

export interface TelegramWebApp {
  initData: string;
  version: string;
  isVersionAtLeast(version: string): boolean;
  ready(): void;
  SecureStorage?: TelegramStorageApi;
  DeviceStorage?: TelegramStorageApi;
}

export function webApp(): TelegramWebApp | undefined {
  return (globalThis as { Telegram?: { WebApp?: TelegramWebApp } }).Telegram?.WebApp;
}

export function stores(app: TelegramWebApp | undefined): { secure: TelegramStore; device: TelegramStore } {
  const modern = app !== undefined && app.isVersionAtLeast("9.0");
  return {
    secure: new TelegramStore(app?.SecureStorage, "SecureStorage", modern && app?.SecureStorage !== undefined),
    device: new TelegramStore(app?.DeviceStorage, "DeviceStorage", modern && app?.DeviceStorage !== undefined),
  };
}
