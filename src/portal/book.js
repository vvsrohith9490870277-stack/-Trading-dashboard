// Book switch for the portal. The live book is paper_v2; paper_v1 is the read-only
// archive (9–18 Nov). The book comes only from the ?book= query param; anything
// else (missing, unknown, demo) means the live book.
export const BOOKS = ['paper_v2', 'paper_v1'];
export const ACTIVE_BOOK = 'paper_v2';
export const ARCHIVE_BOOK = 'paper_v1';
export const ARCHIVE_LABEL = 'Archived book (paper_v1, 9–18 Nov)';

export function bookFromSearch(search) {
  try { return new URLSearchParams(search).get('book') === ARCHIVE_BOOK ? ARCHIVE_BOOK : ACTIVE_BOOK; } catch { return ACTIVE_BOOK; }
}

function parts(href) {
  const u = new URL(href, 'http://localhost');
  return {base: u.pathname, hash: u.hash, params: u.searchParams};
}
function join({base, hash, params}) {
  const q = params.toString();
  return base + (q ? '?' + q : '') + hash;
}

// Adds book=paper_v1 for the archive, removes the param for the active book.
export function withBook(href, book) {
  const p = parts(href);
  if (book === ARCHIVE_BOOK) p.params.set('book', ARCHIVE_BOOK); else p.params.delete('book');
  return join(p);
}

// Always sets the book param explicitly, even for the active book.
export function bookHref(href, book) {
  const p = parts(href);
  p.params.set('book', book);
  return join(p);
}
