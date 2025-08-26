import { PosMenuItem } from '../../models/PosMenuItem';

// Example schema for items coming from Square POS
export interface SquareItem {
  itemName: string;
  categoryName?: string;
  modifiers?: string[];
}

export function mapSquareItem(item: SquareItem): PosMenuItem {
  return {
    canonicalName: item.itemName,
    aliases: [item.itemName],
    category: item.categoryName || 'uncategorized',
    attributes: { modifiers: item.modifiers || [] }
  };
}

export function mapSquareMenu(items: SquareItem[]): PosMenuItem[] {
  return items.map(mapSquareItem);
}
