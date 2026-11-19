// Static contract for scripts/smoke-live.mjs (the script itself needs a live server, so it is
// parsed as text here, not run). The smoke script must assert the active book paper_v2 for
// default reads, check the paper_v1 archive explicitly, and reject bad book values.
import {readFileSync} from 'node:fs';
import {describe, expect, it} from 'vitest';

const script = readFileSync('scripts/smoke-live.mjs', 'utf8');

describe('smoke-live script', () => {
  it('asserts the active book paper_v2 for default reads and history', () => {
    expect(script).toContain(`d.namespace!=='paper_v2'`);
    expect(script).toContain(`hd.namespace!=='paper_v2'`);
  });

  it('checks the paper_v1 archive explicitly and only there', () => {
    expect(script).toContain('book=paper_v1');
    expect(script).toContain(`ad.namespace!=='paper_v1'`);
    expect(script.match(/namespace!=='paper_v1'/g)).toHaveLength(1);
  });

  it('rejects demo and unknown books at the boundary', () => {
    expect(script).toContain(`'?book=paper_demo_v1',400`);
    expect(script).toContain(`'?book=other',400`);
  });
});
