export class StorageUnavailable extends Error {
  constructor(public readonly area: string) {
    super(`${area} is not available in this Telegram client`);
  }
}

export interface KeyValueStore {
  readonly available: boolean;
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  remove(key: string): Promise<void>;
}

type Callback<T> = (error: string | null, value?: T) => void;

export interface TelegramStorageApi {
  setItem(key: string, value: string, callback?: Callback<boolean>): void;
  getItem(key: string, callback: Callback<string | null>): void;
  removeItem(key: string, callback?: Callback<boolean>): void;
}

export class TelegramStore implements KeyValueStore {
  constructor(
    private readonly api: TelegramStorageApi | undefined,
    private readonly area: string,
    readonly available: boolean,
  ) {}

  private call<T>(action: (callback: Callback<T>) => void): Promise<T> {
    const api = this.api;
    if (!this.available || api === undefined) return Promise.reject(new StorageUnavailable(this.area));
    return new Promise<T>((resolve, reject) => {
      action((error, value) => {
        if (error) reject(new StorageUnavailable(`${this.area}: ${error}`));
        else resolve(value as T);
      });
    });
  }

  get(key: string): Promise<string | null> {
    return this.call<string | null>((done) => this.api!.getItem(key, done)).then((value) => value ?? null);
  }

  set(key: string, value: string): Promise<void> {
    return this.call<boolean>((done) => this.api!.setItem(key, value, done)).then(() => undefined);
  }

  remove(key: string): Promise<void> {
    return this.call<boolean>((done) => this.api!.removeItem(key, done)).then(() => undefined);
  }
}

export class MemoryStore implements KeyValueStore {
  readonly values = new Map<string, string>();

  constructor(readonly available: boolean = true) {}

  async get(key: string): Promise<string | null> {
    if (!this.available) throw new StorageUnavailable("memory");
    return this.values.get(key) ?? null;
  }

  async set(key: string, value: string): Promise<void> {
    if (!this.available) throw new StorageUnavailable("memory");
    this.values.set(key, value);
  }

  async remove(key: string): Promise<void> {
    if (!this.available) throw new StorageUnavailable("memory");
    this.values.delete(key);
  }
}
