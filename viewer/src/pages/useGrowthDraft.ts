import { useState } from 'react';

/** Keep unfinished input attached to its project and card, including navigation away. */
export function useGrowthDraft(kind: string, scope: string): [string, (value: string) => void] {
  const storageKey = 'growth:drafts:' + kind;
  const [drafts, setDrafts] = useState<Record<string, string>>(() => {
    try { return JSON.parse(localStorage.getItem(storageKey) ?? '{}') as Record<string, string>; }
    catch { return {}; }
  });
  return [drafts[scope] ?? '', (value: string) => {
    setDrafts((current) => {
      const next = { ...current, [scope]: value };
      try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* Keep the live draft if storage is full. */ }
      return next;
    });
  }];
}
