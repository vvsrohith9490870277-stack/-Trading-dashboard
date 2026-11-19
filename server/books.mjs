// Book allowlist for the dashboard. The active book comes from the repo config
// (automation/paper-trading/config.json); paper_v1 is the read-only archive.
// paper_demo_v1 is never a book: it is only the isolated demo store.
import cfg from '../automation/paper-trading/config.json' with {type: 'json'};

export const BOOKS = ['paper_v2', 'paper_v1'];
export const ARCHIVE_BOOK = 'paper_v1';
export const ACTIVE_BOOK = cfg.namespace;
if (!BOOKS.includes(ACTIVE_BOOK)) throw new Error('ACTIVE_BOOK_NOT_IN_BOOKS');
export function isBook(v) { return typeof v === 'string' && BOOKS.includes(v); }
