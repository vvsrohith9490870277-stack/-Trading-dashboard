// Unit tests for the portal book switch (src/portal/book.js).
import {describe, expect, it} from 'vitest';
import {ACTIVE_BOOK, ARCHIVE_BOOK, ARCHIVE_LABEL, BOOKS, bookFromSearch, bookHref, withBook} from '../src/portal/book';

describe('portal book allowlist', () => {
  it('pins the active book to paper_v2 and the archive to paper_v1', () => {
    expect(BOOKS).toEqual(['paper_v2', 'paper_v1']);
    expect(ACTIVE_BOOK).toBe('paper_v2');
    expect(ARCHIVE_BOOK).toBe('paper_v1');
    expect(ARCHIVE_LABEL).toBe('Archived book (paper_v1, 9–18 Nov)');
  });

  it('bookFromSearch reads only an exact book=paper_v1', () => {
    expect(bookFromSearch('')).toBe('paper_v2');
    expect(bookFromSearch('?book=paper_v2')).toBe('paper_v2');
    expect(bookFromSearch('?book=paper_v1')).toBe('paper_v1');
    expect(bookFromSearch('?foo=1&book=paper_v1')).toBe('paper_v1');
    expect(bookFromSearch('?book=other')).toBe('paper_v2');
    expect(bookFromSearch('?book=paper_demo_v1')).toBe('paper_v2');
    expect(bookFromSearch('?book=PAPER_V1')).toBe('paper_v2');
    expect(bookFromSearch('?book=paper_v1%20OR%201=1')).toBe('paper_v2');
  });

  it('withBook adds book=paper_v1 and removes it for the active book', () => {
    expect(withBook('/paper', 'paper_v1')).toBe('/paper?book=paper_v1');
    expect(withBook('/paper?book=paper_v1', 'paper_v2')).toBe('/paper');
    expect(withBook('/paper?runId=scan-2026-11-09-s3', 'paper_v1')).toBe('/paper?runId=scan-2026-11-09-s3&book=paper_v1');
    expect(withBook('/stock/RELIANCE?runId=scan-2026-11-09-s3&book=paper_v1', 'paper_v2')).toBe('/stock/RELIANCE?runId=scan-2026-11-09-s3');
    expect(withBook('/paper?book=paper_v1', 'paper_v1')).toBe('/paper?book=paper_v1');
  });

  it('bookHref always sets the book param explicitly', () => {
    expect(bookHref('/paper', 'paper_v2')).toBe('/paper?book=paper_v2');
    expect(bookHref('/paper', 'paper_v1')).toBe('/paper?book=paper_v1');
    expect(bookHref('/paper?book=paper_v1', 'paper_v2')).toBe('/paper?book=paper_v2');
  });
});
