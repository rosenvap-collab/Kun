import { PosMenuItem } from '../../models/PosMenuItem';

// Example schema for items coming from Toast POS
export interface ToastItem {
  name: string;
  group: string;
  tags?: string[];
}

// Map a Toast item into the canonical schema
export function mapToastItem(item: ToastItem): PosMenuItem {
  return {
    canonicalName: item.name,
    aliases: [item.name, ...(item.tags || [])],
    category: item.group,
    attributes: { tags: item.tags || [] }
  };
}

export function mapToastMenu(items: ToastItem[]): PosMenuItem[] {
  return items.map(mapToastItem);
}
