import { PosMenuItem } from '../models/PosMenuItem';

/** Tokenize a string into lowercase words */
export function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

/** Calculate Jaccard similarity between two token arrays */
export function scoreSimilarity(a: string[], b: string[]): number {
  const setA = new Set(a);
  const setB = new Set(b);
  const intersection = new Set([...setA].filter(x => setB.has(x)));
  const union = new Set([...setA, ...setB]);
  return union.size === 0 ? 0 : intersection.size / union.size;
}

/** Find best matching canonical item for a given imported item */
export function findBestMatch(
  item: PosMenuItem,
  canonicals: PosMenuItem[]
): { match: PosMenuItem; score: number } | null {
  const tokens = tokenize(item.canonicalName);
  let best: { match: PosMenuItem; score: number } | null = null;
  for (const candidate of canonicals) {
    const candidateTokens = tokenize(candidate.canonicalName);
    const score = scoreSimilarity(tokens, candidateTokens);
    if (!best || score > best.score) {
      best = { match: candidate, score };
    }
  }
  return best;
}

/**
 * Map imported items to their closest canonical equivalents
 * @param imported Items imported from a vendor adapter
 * @param canonicals Canonical menu items
 * @param threshold Minimum similarity score for a match
 */
export function standardizeMenu(
  imported: PosMenuItem[],
  canonicals: PosMenuItem[],
  threshold = 0.5
): Map<PosMenuItem, PosMenuItem> {
  const mapping = new Map<PosMenuItem, PosMenuItem>();
  for (const item of imported) {
    const result = findBestMatch(item, canonicals);
    if (result && result.score >= threshold) {
      mapping.set(item, result.match);
    }
  }
  return mapping;
}
