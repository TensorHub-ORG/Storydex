const DATABASE = "storydex-drafts";
const STORE = "projects";

export function draftProjectKey(root: string): string {
  const path = root.replace(/\\/g, "/").replace(/\/+$/, "");
  return /^[a-z]:\//i.test(path) || path.startsWith("//") ? path.toLowerCase() : path;
}

async function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error("草稿数据库被其他窗口占用，请关闭旧窗口后重试。"));
  });
}

export async function writeProjectDrafts(root: string, drafts: Record<string, unknown>): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(STORE, "readwrite");
      transaction.oncomplete = () => resolve();
      transaction.onabort = () => reject(transaction.error || new Error("草稿备份事务中止。"));
      transaction.onerror = () => reject(transaction.error);
      transaction.objectStore(STORE).put(drafts, draftProjectKey(root));
    });
  } finally {
    database.close();
  }
}

// Only replace documents this window has loaded; unopened drafts survive reloads
// and an empty editor cannot erase the backup before restoration.
export async function updateProjectDrafts(root: string, changes: Record<string, unknown | null>): Promise<void> {
  if (!Object.keys(changes).length) return;
  await accessProjectDrafts(root, changes);
}

export function readProjectDrafts<T>(root: string): Promise<Record<string, T>> {
  return accessProjectDrafts<T>(root);
}

async function accessProjectDrafts<T>(root: string, changes?: Record<string, T | null>): Promise<Record<string, T>> {
  const database = await openDatabase();
  let migratedKey: string | undefined;
  try {
    const drafts = await new Promise<Record<string, T>>((resolve, reject) => {
      // Check and migrate atomically: two windows opening together must not
      // overwrite a newer backup with the same legacy localStorage snapshot.
      const transaction = database.transaction(STORE, "readwrite");
      const store = transaction.objectStore(STORE);
      let stored: Record<string, T> = {};
      transaction.oncomplete = () => resolve(stored);
      transaction.onabort = () => reject(transaction.error || new Error("草稿读取事务中止。"));
      transaction.onerror = () => reject(transaction.error);
      const request = store.get(draftProjectKey(root));
      request.onsuccess = () => {
        try {
          if (request.result !== undefined) {
            stored = request.result;
          } else {
            const legacyKey = `storydex.drafts:${root.replace(/\\/g, "/").toLowerCase()}`;
            const raw = localStorage.getItem(legacyKey);
            if (raw) {
              stored = JSON.parse(raw) as Record<string, T>;
              migratedKey = legacyKey;
            }
          }
          for (const [path, draft] of Object.entries(changes || {})) {
            if (draft === null) delete stored[path];
            else stored[path] = draft;
          }
          if (migratedKey || changes) store.put(stored, draftProjectKey(root));
        } catch (error) {
          transaction.abort();
          reject(error);
        }
      };
    });
    // A failed transaction leaves the old copy available for the next attempt.
    if (migratedKey) localStorage.removeItem(migratedKey);
    return drafts;
  } finally {
    database.close();
  }
}
