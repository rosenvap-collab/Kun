export interface PosMenuItem {
  /** Standardized name of the menu item */
  canonicalName: string;
  /** Alternative names observed in vendor systems */
  aliases: string[];
  /** Category such as 'entree', 'drink', etc. */
  category: string;
  /** Additional attributes like size, modifiers, or tags */
  attributes: Record<string, unknown>;
}
